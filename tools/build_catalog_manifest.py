"""Maintainer-only builder for the pinned CC0 game-audio catalogue.

Fetches the explicitly listed authors' pages and public ZIPs, validates archive
members and decodes each candidate with FFmpeg. Media lives in the OS temporary
directory; only names, hashes, provenance and measured metadata enter Git.
Run: python tools/build_catalog_manifest.py
There is deliberately no arbitrary URL option or runtime crawling endpoint.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import hashlib
import html
import json
import math
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CC0 = "https://creativecommons.org/publicdomain/zero/1.0/"
MAX_ARCHIVE = 160 * 1024 * 1024
MAX_MEMBER = 50 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
EXTENSIONS = {".wav", ".ogg", ".mp3", ".flac", ".m4a", ".aac"}

# A concrete game category is allowed as a common tag; keywords for individual
# materials/actions are taken from the file's own path, never the whole pack.
SEEDS = [
    ("kenney-rpg", "Kenney RPG 拟音", "RPG Audio", "Kenney", "kenney", "rpg-audio", "衣物、书本、门、刀具与物品操作等 RPG 短音效。", ["rpg", "拟音", "foley"]),
    ("kenney-scifi", "Kenney 科幻音效", "Sci-fi Sounds", "Kenney", "kenney", "sci-fi-sounds", "飞船、引擎、科幻装置与太空音效。", ["科幻", "sci-fi"]),
    ("kenney-ui", "Kenney UI 按钮", "UI Audio", "Kenney", "kenney", "ui-audio", "按钮、开关与界面点击短音效。", ["界面", "ui"]),
    ("kenney-casino", "Kenney 纸牌与骰子", "Casino Audio", "Kenney", "kenney", "casino-audio", "纸牌、骰子、筹码等桌游操作拟音。", ["桌游", "casino"]),
    ("kenney-fighter", "Kenney 战斗人声", "Voiceover Pack (Fighter)", "Kenney", "kenney", "voiceover-pack-fighter", "英语战斗角色喊声、反应与短台词。", ["人声", "voice", "战斗", "fighter"]),
    ("kenney-voice", "Kenney 游戏播报", "Voiceover Pack", "Kenney", "kenney", "voiceover-pack", "游戏播报和操作反馈英语短台词。", ["人声", "voice", "播报", "announcer"]),
    ("kenney-jingles", "Kenney 短提示乐句", "Music Jingles", "Kenney", "kenney", "music-jingles", "关卡结果、胜利、奖励等短乐句；不作为长篇背景音乐。", ["乐句", "jingle"]),
    ("oga-rpg", "Rubberduck RPG 物品与法术", "80 CC0 RPG SFX", "rubberduck", "opengameart", "80-cc0-rpg-sfx", "刀刃、翻书、锁、道具、石块、木材和法术音效。", ["rpg", "拟音", "foley"]),
    ("oga-creatures", "Rubberduck 生物声音", "80 CC0 creature SFX", "rubberduck", "opengameart", "80-cc0-creature-sfx", "作者表演并加工的怪物、生物和异形短声音。", ["生物", "creature", "人声", "voice"]),
    ("oga-creatures2", "Rubberduck 生物声音 2", "80 CC0 creture SFX #2", "rubberduck", "opengameart", "80-cc0-creture-sfx-2", "第二套生物、怪物与角色反应声音。", ["生物", "creature", "人声", "voice"]),
    ("oga-materials", "Rubberduck 金属与木材", "100 CC0 metal and wood SFX", "rubberduck", "opengameart", "100-cc0-metal-and-wood-sfx", "金属和木质物体操作、落地、碰撞与冲击拟音。", ["拟音", "foley"]),
    ("oga-foley2", "Rubberduck 日常拟音与环境", "100 CC0 SFX #2", "rubberduck", "opengameart", "100-cc0-sfx-2", "水、石头、玻璃、机械、门、街道和环境等多种原始拟音。", ["拟音", "foley"]),
    ("oga-breaks", "Rubberduck 破碎与落地", "75 CC0 breaking / falling / hit sfx", "rubberduck", "opengameart", "75-cc0-breaking-falling-hit-sfx", "木材、金属、玻璃、岩石的破碎、落下和击打。", ["拟音", "foley"]),
    ("oga-scifi", "Rubberduck 科幻音效", "50 CC0 Sci-Fi SFX", "rubberduck", "opengameart", "50-cc0-sci-fi-sfx", "科幻机器、能量、信号与装置的短声音。", ["科幻", "sci-fi"]),
    ("oga-water", "Rubberduck 水与黏液", "40 CC0 water / splash / slime SFX", "rubberduck", "opengameart", "40-cc0-water-splash-slime-sfx", "气泡、雨、水循环、水花和黏液音效，部分来自实物录音。", ["拟音", "foley"]),
    ("oga-loops", "Rubberduck 环境循环", "30 CC0 SFX loops", "rubberduck", "opengameart", "30-cc0-sfx-loops", "水流、雨、机械、警报等可循环环境声音。", ["循环", "loop", "环境", "ambience"]),
    ("oga-keyboard", "Unicae 键盘拟音", "Keyboard Soundpack #1 [Typing and Single Keystrokes]", "unicaegames", "opengameart", "keyboard-soundpack-1-typing-and-single-keystrokes", "Cherry KC 1000 键盘录音；作者说明使用 Shure SM7B 与 Izotope Neutron 2。", ["键盘", "keyboard", "拟音", "foley"]),
    ("oga-monsters", "Ogrebane 怪物声音", "Monster Sound Effects 2", "Ogrebane", "opengameart", "monster-sound-effects-2", "怪物呼吸、吼叫和反应声音。", ["怪物", "monster", "人声", "voice"]),
    ("oga-dark", "Ogrebane 阴暗环境", "Dark Ambiences", "Ogrebane", "opengameart", "dark-ambiences", "适合阴暗场景和恐怖游戏的环境素材。", ["环境", "ambience", "恐怖", "horror"]),
    ("oga-footsteps", "TinyWorlds 多材质脚步", "Different steps on wood, stone, leaves, gravel and mud", "TinyWorlds", "opengameart", "different-steps-on-wood-stone-leaves-gravel-and-mud", "木材、石头、树叶、砂砾和泥地等表面的脚步录音。", ["脚步", "footstep", "拟音", "foley"]),
    ("oga-armorsteps", "HaelDB 衣物与盔甲脚步", "Footsteps Leather, Cloth, Armor", "HaelDB", "opengameart", "footsteps-leather-cloth-armor", "皮革、布料和盔甲角色脚步；按作者给出的 CC0 选项使用。", ["脚步", "footstep", "拟音", "foley"]),
    ("oga-zombies", "Summoning Wars 僵尸声音", "Zombies Sound Pack", "artisticdude", "opengameart", "zombies-sound-pack", "为开源 RPG Summoning Wars 制作的 24 条僵尸 WAV。", ["僵尸", "zombie", "人声", "voice"]),
    ("oga-swishes", "Summoning Wars 武器挥动", "Swishes Sound Pack", "artisticdude", "opengameart", "swishes-sound-pack", "为 Summoning Wars 制作的 13 条轻重武器挥动 WAV。", ["挥动", "whoosh", "武器", "weapon"]),
    ("oga-shop", "LEGIT Audio 工坊试用素材", "The Shop", "LEGIT Audio", "opengameart", "the-shop", "专业工坊音效包的免费 WAV 子集；CC0 只覆盖本页面提供的免费文件。", ["拟音", "foley", "工坊", "workshop"]),
    ("oga-owlish", "Owlish Media 实录与拟音", "Sound Effects Pack", "OwlishMedia", "opengameart", "sound-effects-pack", "作者多年录制的环境、布料、脚步、人声、物体碰撞、纸张、科技与水声。首次试听需要下载约 142 MiB 原包。", ["拟音", "foley"]),
    ("oga-owlish202", "Owlish Media 生活拟音 202", "202 More Sound Effects", "OwlishMedia", "opengameart", "202-more-sound-effects", "家居录音：相机、衣服、饮水、钥匙与锁、门、厨具、钱币、纸张、塑料、文具和魔术贴。", ["拟音", "foley"]),
    ("oga-superpowers", "Sparklin Labs 游戏事件", "Superpowers assets sound effects", "Sparklin Labs (submitted by MedicineStorm)", "opengameart", "superpowers-assets-sound-effects", "开源 Superpowers 游戏工具的游戏事件音效；按场景与原始文件名检索。", ["游戏", "game"]),
    ("oga-retro512", "SubspaceAudio 复古游戏音效", "512 Sound Effects (8-bit style)", "SubspaceAudio", "opengameart", "512-sound-effects-8-bit-style", "按用途组织的 512 条复古游戏音效；像素风格与合成音色，不表示真实世界录音。", ["复古", "retro", "8-bit"]),
]

WORDS = {
    "alien":"异形", "armor":"盔甲", "armour":"盔甲", "attack":"攻击", "bark":"犬吠", "bell":"铃声", "belt":"腰带", "blade":"刀刃", "book":"书本", "breath":"呼吸", "bubble":"气泡", "burble":"咕噜", "burp":"打嗝", "button":"按钮", "card":"纸牌", "casino":"桌游", "chain":"锁链", "chip":"筹码", "chop":"劈砍", "click":"点击", "close":"关闭", "cloth":"布料", "coin":"金币", "construction":"施工", "cough":"咳嗽", "crack":"裂开", "creak":"吱呀", "creature":"生物", "cute":"可爱", "dark":"阴暗", "death":"死亡", "dice":"骰子", "die":"死亡", "digital":"电子", "dirt":"泥土", "door":"门", "down":"下降", "draw":"抽出", "drop":"落下", "eat":"进食", "engine":"引擎", "error":"错误", "explosion":"爆炸", "fail":"失败", "fall":"落下", "fighter":"战斗", "fire":"火焰", "flip":"翻动", "flow":"流动", "footstep":"脚步", "gem":"宝石", "glass":"玻璃", "gravel":"砂砾", "ground":"地面", "grunt":"低吼", "handle":"操作", "hit":"击打", "howl":"嚎叫", "hurt":"受伤", "impact":"冲击", "item":"物品", "jingle":"乐句", "jump":"跳跃", "key":"按键", "knife":"小刀", "laser":"激光", "leaf":"树叶", "leaves":"树叶", "leather":"皮革", "lock":"锁", "loop":"循环", "lose":"失败", "low":"低沉", "machine":"机械", "magic":"魔法", "menu":"菜单", "metal":"金属", "monster":"怪物", "mud":"泥地", "noise":"噪声", "notification":"提示", "open":"打开", "page":"书页", "paper":"纸张", "pain":"疼痛", "place":"放下", "power":"能量", "rain":"雨", "roar":"咆哮", "rock":"岩石", "roll":"滚动", "sci":"科幻", "scream":"尖叫", "select":"选择", "shield":"护盾", "shot":"射击", "slime":"黏液", "snow":"雪", "snore":"鼾声", "space":"太空", "spell":"法术", "spit":"吐出", "splash":"水花", "step":"脚步", "stone":"石头", "success":"成功", "swish":"挥动", "switch":"开关", "sword":"剑", "thunder":"雷声", "tool":"工具", "typing":"打字", "up":"上升", "victory":"胜利", "walk":"行走", "water":"水", "weapon":"武器", "win":"胜利", "wind":"风", "wood":"木材", "wooden":"木材", "zombie":"僵尸", "grind":"磨削", "hammer":"锤击", "drill":"钻孔", "saw":"锯切", "slam":"猛关", "rattle":"碰撞", "switches":"开关", "street":"街道", "highway":"公路", "ambient":"环境", "switching":"开关", "swing":"挥动", "goblin":"哥布林", "scifi":"科幻", "beep":"哔声", "burble":"咕噜", "level":"关卡", "collect":"收集", "pickup":"拾取", "powerup":"强化", "shoot":"射击", "damage":"伤害", "synth":"合成", "break":"破碎", "breaking":"破碎", "falling":"落下", "alarm":"警报", "boiled":"沸腾", "flesh":"肉体", "crunch":"碎裂", "march":"行进", "thud":"闷响", "gui":"界面", "robot":"机器人", "hurted":"受伤", "stones":"石头", "footsteps":"脚步", "woodfall":"木材落下", "metalhit":"金属击打", "clang":"金属碰撞", "chatter":"叽喳", "doorbell":"门铃", "spark":"电火花", "accept":"确认", "decline":"取消", "confirm":"确认", "cancel":"取消", "running":"奔跑", "grunts":"低吼", "growl":"低吼", "gasps":"喘息", "laugh":"笑声", "bong":"提示", "creak":"吱呀", "leap":"跃起", "punch":"拳击", "damage":"受伤", "increase":"提升", "decrease":"降低", "teleport":"传送", "movement":"移动", "toggle":"切换", "default":"默认", "drip":"滴水", "knock":"敲击", "cheer":"欢呼", "squeak":"吱响", "roaring":"咆哮", "hiss":"嘶声", "swishes":"挥动", "light":"轻", "heavy":"重", "boots":"靴子", "shoes":"鞋", "steel":"钢铁", "skeleton":"骷髅", "plant":"植物", "ring":"铃声", "troll":"巨魔", "bubbles":"气泡", "keys":"按键", "squelch":"湿黏", "growls":"低吼", "beast":"野兽", "grunting":"低吼", "moan":"呻吟", "monster":"怪物"
}
WORDS.update({"female": "女声", "male": "男声", "numbers": "数字", "cloths": "布料", "lth": "皮革", "camera": "相机", "velcro": "魔术贴", "scissors": "剪刀", "tape": "胶带", "stapler": "订书机", "plastic": "塑料", "drink": "饮水", "swallow": "吞咽", "phone": "电话", "cash": "现金", "money": "钱币", "jar": "罐子", "pot": "锅", "toaster": "烤面包机", "pull": "拉动", "push": "推动", "drag": "拖动", "ceramic": "陶瓷", "fabric": "布料", "swipe": "划动", "dull": "闷响", "rustle": "沙沙", "rustling": "沙沙", "microwave": "微波炉", "unlock": "解锁", "sniff": "吸鼻", "fan": "风扇", "spinner": "旋转", "drawer": "抽屉", "typing": "打字", "keystroke": "按键", "broken": "破碎", "looped": "循环", "metallic": "金属", "crackle": "噼啪", "gravelly": "砂砾", "dripping": "滴水", "woodfloor": "木地板"})
WORDS.update({"air": "空气", "misc": "杂项", "other": "其他", "unknown": "未分类"})


def safe_url(value: str, archive: bool = False) -> str:
    parts = urlsplit(value)
    decoded = unquote(parts.path)
    if (parts.scheme != "https" or parts.hostname not in {"kenney.nl", "opengameart.org"}
            or parts.port not in (None, 443) or parts.username or parts.password
            or parts.query or parts.fragment or "\\" in decoded
            or any(part == ".." for part in PurePosixPath(decoded).parts)):
        raise ValueError("URL is outside the explicit official-source allowlist")
    if archive and (not parts.path.lower().endswith(".zip") or not (
            parts.hostname == "kenney.nl" and parts.path.startswith("/media/pages/assets/")
            or parts.hostname == "opengameart.org" and parts.path.startswith("/sites/default/files/"))):
        raise ValueError("Not an official public ZIP")
    return value


class RestrictedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url(newurl, req.full_url.lower().endswith(".zip"))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, limit: int = MAX_ARCHIVE) -> bytes:
    safe_url(url)
    with build_opener(RestrictedRedirect()).open(Request(url, headers={"User-Agent": "Soundbox-Catalog-Maintainer/1.0", "Accept": "*/*"}), timeout=90) as response:
        safe_url(response.url)
        chunks, total = [], 0
        while chunk := response.read(65536):
            total += len(chunk)
            if total > limit:
                raise ValueError("Source response exceeds the byte budget")
            chunks.append(chunk)
        return b"".join(chunks)


def tokenize(path: str) -> list[str]:
    # Strip generic root folders such as 80-CC0-RPG-SFX, but keep meaningful
    # subfolders such as wood/stone and the original filename.
    components = PurePosixPath(path).with_suffix("").parts
    meaningful = [p for p in components if not re.search(r"cc0|kenney|sound.?effects|essential|collection|soundpack|^audio$|^sfx$|^sounds?$", p, re.I)]
    value = " ".join(meaningful) or PurePosixPath(path).stem
    value = re.sub(r"([a-z])([A-Z])", r"\1 \2", value)
    return [word.lower() for word in re.findall(r"[a-zA-Z]+", value) if len(word) >= 2]


def labels(path: str, common: list[str], pack_name: str) -> tuple[str, list[str]]:
    tokens = list(dict.fromkeys(tokenize(path)))
    mapping = WORDS
    if "casino" in common and "card" in tokens and "fan" in tokens:
        mapping = WORDS | {"fan": "扇牌"}
    translated = list(dict.fromkeys(mapping.get(token, mapping.get(token.rstrip("s"))) for token in tokens if token in mapping or token.rstrip("s") in mapping))
    # A multifunction pack's display name describes its whole inventory, never
    # an unclassified individual sound. Putting it here would make misc_01
    # appear in searches for spells/doors merely because its pack contains them.
    title = " / ".join(translated[:4]) if translated else "杂项音效"
    original = " / ".join(PurePosixPath(path).with_suffix("").parts[-2:])
    tags = list(dict.fromkeys(common + tokens + translated))[:32]
    return f"{title} · {original}", tags


def command(args: list[str], timeout: int = 60) -> bytes:
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", "replace")[:200])
    return result.stdout


def inspect_member(info: zipfile.ZipInfo, data: bytes, common: list[str], name: str, ffprobe: str, ffmpeg: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="soundbox-probe-") as work:
        path = Path(work) / ("audio" + PurePosixPath(info.filename).suffix.lower())
        path.write_bytes(data)
        metadata = json.loads(command([ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries", "format=duration:stream=codec_type,codec_name,sample_rate,channels", "-of", "json", str(path)]))
        duration = float(metadata["format"]["duration"])
        if not math.isfinite(duration) or not 0 < duration <= 600:
            raise ValueError("Unsupported duration")
        audio = next(stream for stream in metadata["streams"] if stream.get("codec_type") == "audio")
        command([ffmpeg, "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe", "-i", str(path), "-vn", "-f", "null", "-"], timeout=90)
        title, tags = labels(info.filename, common, name)
        return {"path": info.filename, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "name": title, "tags": tags, "duration": round(duration, 6), "format": path.suffix[1:], "sample_rate": int(audio.get("sample_rate", 0)), "channels": int(audio.get("channels", 0)), "codec": audio.get("codec_name", "unknown")}


def validate_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    seen, total, files = set(), 0, []
    if len(archive.infolist()) > 10000:
        raise ValueError("Too many ZIP members")
    for info in archive.infolist():
        path = PurePosixPath(info.filename)
        mode = info.external_attr >> 16
        original_path = getattr(info, "orig_filename", info.filename)
        if (path.is_absolute() or ".." in path.parts or "\\" in original_path or ":" in info.filename
                or stat.S_ISLNK(mode) or info.flag_bits & 1 or info.filename in seen):
            raise ValueError("Unsafe ZIP member: " + info.filename)
        seen.add(info.filename)
        total += info.file_size
        if total > MAX_TOTAL:
            raise ValueError("ZIP size budget exceeded")
        if info.is_dir() or any(p.startswith(".") or p == "__MACOSX" for p in path.parts):
            continue
        if path.suffix.lower() not in EXTENSIONS or info.file_size > MAX_MEMBER:
            continue
        # Public demo mixes aren't independently reusable game events.
        if re.search(r"(?:^|[/_ -])(preview|demo|showcase|mixdown)(?:[/_ .-]|$)", info.filename, re.I):
            continue
        files.append(info)
    return files


def build_pack(seed: tuple, cache: Path, workers: int) -> tuple[dict, list[str]]:
    identifier, name, original, author, origin, slug, description, common = seed
    source = ("https://kenney.nl/assets/" if origin == "kenney" else "https://opengameart.org/content/") + slug
    page_bytes = fetch(source, 4 * 1024 * 1024)
    page = page_bytes.decode("utf-8")
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))
    licenses = re.search(r"License(?:\(s\))?:?\s+(.*?)(?:Collections:|Download)", text)
    if not licenses or "CC0" not in licenses.group(1):
        raise ValueError(f"{identifier}: CC0 was not found in the actual license field")
    links = list(dict.fromkeys(html.unescape(x) for x in re.findall(r"href=[\"']([^\"']+)", page) if urlsplit(html.unescape(x)).path.lower().endswith(".zip")))
    # The Shop offers identical WAV/MP3 sample bundles; select the WAV source.
    if identifier == "oga-shop":
        links = [x for x in links if "_wav.zip" in x]
    if len(links) != 1:
        raise ValueError(f"{identifier}: expected one explicit public ZIP, got {len(links)}")
    download = safe_url(urljoin(source, links[0]), archive=True)
    cache_path = cache / (identifier + ".zip")
    cache_meta = cache_path.with_suffix(".json")
    # Reuse media to resume probes; --refresh removes this known temp cache.
    data = None
    if cache_path.exists() and cache_meta.exists():
        cached = json.loads(cache_meta.read_text(encoding="utf-8"))
        candidate = cache_path.read_bytes()
        if cached.get("download_url") == download and cached.get("sha256") == hashlib.sha256(candidate).hexdigest():
            data = candidate
    if data is None:
        data = fetch(download)
        cache_path.write_bytes(data)
        cache_meta.write_text(json.dumps({"download_url": download, "sha256": hashlib.sha256(data).hexdigest()}), encoding="utf-8")
    if not 0 < len(data) <= MAX_ARCHIVE:
        raise ValueError("Unsupported archive byte count")
    ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        raise ValueError("Install FFmpeg before building measured catalogue metadata")
    exclusions = []
    with zipfile.ZipFile(cache_path) as archive:
        members = validate_members(archive)
        tasks = [(info, archive.read(info), common, name, ffprobe, ffmpeg) for info in members]
    def decode(task):
        try:
            return inspect_member(*task)
        except (ValueError, KeyError, TypeError, StopIteration, subprocess.TimeoutExpired) as error:
            exclusions.append(task[0].filename + ": " + str(error)[:100])
            return None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        files = [entry for entry in pool.map(decode, tasks) if entry]
    if not files:
        raise ValueError(f"{identifier}: no decodable short audio files")
    pack = {"id": identifier, "name": name, "original_name": original, "description": description, "author": author, "origin_source": origin, "source_url": source, "download_url": download, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "license": "CC0", "license_url": CC0, "license_evidence_url": source, "license_evidence_sha256": hashlib.sha256(page_bytes).hexdigest(), "files": sorted(files, key=lambda item: item["path"])}
    return pack, exclusions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", action="append", choices=[s[0] for s in SEEDS], help="Known package ID, repeatable")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--refresh", action="store_true", help="Redownload the known temporary archive files")
    parser.add_argument("--output", type=Path, default=ROOT / "server" / "catalog_manifest.json")
    args = parser.parse_args()
    cache = Path(tempfile.gettempdir()) / "soundbox-catalog-build"
    cache.mkdir(exist_ok=True)
    selected = [seed for seed in SEEDS if not args.pack or seed[0] in args.pack]
    packs, excluded = [], {}
    for seed in selected:
        if args.refresh:
            (cache / (seed[0] + ".zip")).unlink(missing_ok=True)
        pack, warnings = build_pack(seed, cache, min(12, max(1, args.workers)))
        packs.append(pack)
        if warnings:
            excluded[pack["id"]] = warnings
        print(f"{pack['id']}: {len(pack['files'])} decoded files, {pack['bytes']} archive bytes", flush=True)
    if args.pack and args.output.exists():
        existing = json.loads(args.output.read_text(encoding="utf-8"))["packs"]
        replaced = {pack["id"] for pack in packs}
        packs = [pack for pack in existing if pack["id"] not in replaced] + packs
    manifest = {"version": 1, "verified_at": date.today().isoformat(), "packs": packs}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(f"Total: {len(packs)} packs / {sum(len(p['files']) for p in packs)} files. Output: {args.output}", flush=True)
    if excluded:
        print("Excluded: " + json.dumps(excluded, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
