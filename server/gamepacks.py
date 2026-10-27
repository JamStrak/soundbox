"""Pinned Kenney CC0 packs, installed locally without trusting browser URLs.

Only a completed, decoded file is committed to Library. Cancellation keeps prior
completed entries, so another install resumes without replacing personal edits.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import threading
from urllib.parse import urlsplit
import uuid
import zipfile
import zlib

import httpx

from .audio import base_sound, inspect_audio
from . import providers
from .search import expand_query


MANIFEST = json.loads(Path(__file__).with_name("gamepacks_manifest.json").read_text(encoding="utf-8"))
PACKS = {pack["id"]: pack for pack in MANIFEST["packs"]}
MAX_ARCHIVE_BYTES = 5 * 1024 * 1024
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_UNPACKED_BYTES = 32 * 1024 * 1024
MAX_MEMBERS = 512
_INSTALL_LOCK = threading.Lock()


class PackCancelled(ValueError):
    """Cooperative cancellation; already committed sounds remain available."""


def _checkpoint(cancelled):
    if cancelled is not None and cancelled():
        raise PackCancelled("已取消导入，已完成的音效已保留。")


def _report(progress, completed, total, message):
    if progress is not None:
        progress(completed, total, message)


def _identifier(pack, entry):
    return "kenney-" + pack["id"] + "-" + PurePosixPath(entry["path"]).stem.lower()


def _group(entry):
    return re.sub(r"[_-]?\d+$", "", PurePosixPath(entry["path"]).stem)


def list_packs(library) -> list[dict]:
    """No network or decoding. Count expected entries that have local files."""
    sounds = {sound["id"]: sound for sound in library.all()}
    result = []
    for pack in PACKS.values():
        installed = 0
        for entry in pack["files"]:
            identifier = _identifier(pack, entry)
            sound = sounds.get(identifier)
            if (sound and sound.get("source") == "kenney" and sound.get("pack_id") == pack["id"]
                    and library.file(identifier) is not None):
                installed += 1
        result.append({key: pack[key] for key in ("id", "name", "description", "count", "bytes")}
                      | {"installed_count": installed})
    return result


def _safe_url(url):
    try:
        parts = urlsplit(url)
        valid = (parts.scheme == "https" and parts.hostname == "kenney.nl"
                 and parts.port in (None, 443) and not parts.username and not parts.password
                 and not parts.query and not parts.fragment
                 and parts.path.startswith("/media/pages/assets/") and parts.path.endswith(".zip")
                 and ".." not in parts.path and "\\" not in url)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("音效包下载地址未经验证，已停止下载。")
    return url


def _download(pack, progress, cancelled):
    url = _safe_url(pack["download_url"])
    expected = pack["bytes"]
    if not isinstance(expected, int) or not 0 < expected <= MAX_ARCHIVE_BYTES:
        raise ValueError("音效包大小超出支持范围。")
    _checkpoint(cancelled)
    _report(progress, 0, pack["count"], "正在下载官方音效包…")
    try:
        with providers._client() as client:
            with client.stream("GET", url, headers={"Accept": "application/zip"}) as response:
                if response.status_code != 200:
                    if response.status_code == 429:
                        raise providers.ProviderError("Kenney 下载暂时限流，请稍后重试。", 429)
                    raise providers.ProviderError("Kenney 官方下载暂时不可用，请稍后重试。", 502)
                length = response.headers.get("content-length")
                if length is not None and (not length.isdigit() or int(length) != expected):
                    raise ValueError("音效包大小与已核实版本不同，请等待更新包清单。")
                data = bytearray()
                for chunk in response.iter_bytes(65536):
                    _checkpoint(cancelled)
                    data.extend(chunk)
                    if len(data) > expected:
                        raise ValueError("音效包超过已核实大小，已停止下载。")
                    _report(progress, 0, pack["count"], f"正在下载官方音效包：{len(data)}/{expected} 字节")
    except httpx.HTTPError as error:
        raise providers._network_error(error, "Kenney 官方音效包") from None
    _checkpoint(cancelled)
    return bytes(data)


def _verified_files(data, pack, cancelled):
    if (len(data) != pack["bytes"] or len(data) > MAX_ARCHIVE_BYTES
            or hashlib.sha256(data).hexdigest() != pack["sha256"]):
        raise ValueError("音效包校验失败，文件可能不完整或官方版本已变化；未导入。")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > MAX_MEMBERS:
                raise ValueError("音效包成员数量超出限制。")
            names, total = {}, 0
            for member in members:
                _checkpoint(cancelled)
                name = member.filename
                components = name.rstrip("/").split("/")
                mode = stat.S_IFMT(member.external_attr >> 16)
                if (not name or member.orig_filename != name or name.startswith("/") or "\\" in name
                        or any(part in {"", ".", ".."} or ":" in part or part.rstrip(" .") != part
                               or any(ord(char) < 32 for char in part) for part in components)
                        or name.casefold() in names or mode not in {0, stat.S_IFREG, stat.S_IFDIR}
                        or member.flag_bits & 1 or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
                    raise ValueError("音效包包含不安全的路径、链接或重复成员，未导入。")
                names[name.casefold()] = member
                total += member.file_size
                if member.file_size > MAX_MEMBER_BYTES or total > MAX_UNPACKED_BYTES:
                    raise ValueError("音效包解压体积超出限制，未导入。")
            license_member = names.get("license.txt")
            if not license_member or license_member.file_size > 16384:
                raise ValueError("音效包缺少可核实的许可文件，未导入。")
            license_bytes = archive.read(license_member)
            license_text = license_bytes.decode("utf-8-sig").lower()
            if (hashlib.sha256(license_bytes).hexdigest() != pack["license_sha256"]
                    or "cc0" not in license_text or "kenney" not in license_text
                    or "creativecommons.org/publicdomain/zero/1.0/" not in license_text):
                raise ValueError("音效包许可与已核实的 Kenney CC0 许可不一致，未导入。")
            expected = {item["path"] for item in pack["files"]}
            actual = {item.filename for item in members if not item.is_dir()
                      and item.filename.startswith("Audio/") and Path(item.filename).suffix.lower() in {".ogg", ".wav", ".mp3", ".flac"}}
            if len(expected) != pack["count"] or actual != expected:
                raise ValueError("音效包音频目录与已核实清单不一致，未导入。")
            verified = {}
            for entry in pack["files"]:
                _checkpoint(cancelled)
                content = archive.read(entry["path"])
                if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                    raise ValueError("音效包内的音频校验失败，未导入。")
                verified[entry["path"]] = content
            return verified
    except (zipfile.BadZipFile, zipfile.LargeZipFile, UnicodeError, RuntimeError, NotImplementedError, EOFError, zlib.error):
        raise ValueError("音效包无法安全读取，文件可能损坏；未导入。") from None


def _existing(library, pack, entry):
    identifier = _identifier(pack, entry)
    sound = library.get(identifier)
    if sound and (sound.get("source") != "kenney" or sound.get("pack_id") != pack["id"]):
        raise ValueError("素材编号与现有记录冲突，未覆盖原素材。")
    path = library.file(identifier) if sound else None
    if path is not None:
        try:
            if path.stat().st_size <= MAX_MEMBER_BYTES and hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]:
                return sound
        except OSError:
            pass
    return None


def _metadata(pack, entry, analysis):
    group = _group(entry)
    description = pack["groups"][group]
    stem = PurePosixPath(entry["path"]).stem
    english_name = re.sub(r"([a-z])([A-Z])", r"\1 \2", stem).replace("_", " ")
    tags = description["tags"]
    # The same canonical vocabulary backs online searches and local matching.
    tags = list(dict.fromkeys([*tags, *expand_query(" ".join(tags))["terms"]]))[:40]
    sound = base_sound(_identifier(pack, entry), description["name"] + " · " + english_name,
                       "kenney", PurePosixPath(entry["path"]).suffix) | analysis
    sound.update({
        "tags": tags, "description": f"Kenney {pack['original_name']}：{description['name']}。官方原始音效。",
        "source_file": entry["path"], "source_id": pack["id"] + "/" + entry["path"],
        "pack_id": pack["id"], "pack_name": pack["name"],
        "variant_group": "kenney-" + pack["id"] + "-" + group.lower(),
        "origin_source": "kenney", "quality": "original", "author": pack["author"],
        "license": pack["license"], "license_url": pack["license_url"],
        "source_url": pack["source_url"], "download_url": pack["download_url"],
        "attribution": f"{pack['original_name']} / {stem} by Kenney — CC0 1.0. {pack['source_url']}",
        "content_sha256": entry["sha256"],
    })
    return sound


def _audio_check(path):
    try:
        module = importlib.import_module(".quality", __package__)
        check = module.analyze_audio(path)
        return {"audio_check": check}
    except ModuleNotFoundError as error:
        if error.name != __package__ + ".quality":
            raise
        return {"audio_check_error": "音频检查待完成：检查模块尚不可用。"}
    except ValueError:
        return {"audio_check_error": "音频检查待完成：请稍后重新检查。"}


def install_pack(pack_id, library, progress=None, cancelled=None) -> list[dict]:
    """Return all pack sounds; PackCancelled keeps earlier completed imports.

    progress(completed, total, message) includes download/verification/analysis.
    Cancellation is checked between chunks and expensive local processing steps;
    a currently running decoder finishes before cancellation takes effect.
    """
    if not isinstance(pack_id, str) or pack_id not in PACKS:
        raise ValueError("未知的游戏音效包，请刷新列表后重试。")
    if not _INSTALL_LOCK.acquire(blocking=False):
        raise providers.ProviderError("已有音效包正在导入，请等待完成或取消后再试。", 409)
    pack = PACKS[pack_id]
    count = pack["count"]
    try:
        _checkpoint(cancelled)
        _report(progress, 0, count, "正在检查已安装音效…")
        missing = False
        for entry in pack["files"]:
            _checkpoint(cancelled)
            if _existing(library, pack, entry) is None:
                missing = True
                break
        files = None
        if missing:
            data = _download(pack, progress, cancelled)
            _report(progress, 0, count, "正在验证文件哈希、许可与音频目录…")
            files = _verified_files(data, pack, cancelled)
        sounds = []
        with tempfile.TemporaryDirectory(prefix=".kenney-", dir=library.media) as temporary:
            for index, entry in enumerate(pack["files"]):
                _checkpoint(cancelled)
                existing = _existing(library, pack, entry)
                if existing is not None:
                    sounds.append(deepcopy(existing))
                    _report(progress, index + 1, count, f"已保留音效 {index + 1}/{count}")
                    continue
                # A file can disappear after the first inventory, e.g. another
                # process removes it. Never silently report a complete install.
                if files is None:
                    data = _download(pack, progress, cancelled)
                    files = _verified_files(data, pack, cancelled)
                name = PurePosixPath(entry["path"]).name
                staged = Path(temporary) / name
                staged.write_bytes(files[entry["path"]])
                _report(progress, index, count, f"正在解析音效 {index + 1}/{count}：{name}")
                analysis = inspect_audio(staged)
                _checkpoint(cancelled)
                check = _audio_check(staged)
                _checkpoint(cancelled)
                sound = _metadata(pack, entry, analysis) | check
                target = library.media / (sound["id"] + "-" + uuid.uuid4().hex[:12] + staged.suffix)
                committed = False
                try:
                    with library.lock:
                        _checkpoint(cancelled)
                        previous = library.get(sound["id"])
                        if previous:
                            if previous.get("source") != "kenney" or previous.get("pack_id") != pack_id:
                                raise ValueError("素材编号冲突，未覆盖原素材。")
                            for field in ("name", "tags", "note", "favorite", "folder_id", "created_at"):
                                if field in previous:
                                    sound[field] = deepcopy(previous[field])
                        os.replace(staged, target)
                        if "audio_check" in sound:
                            info = target.stat()
                            sound["audio_check_stamp"] = f"{info.st_size}:{info.st_mtime_ns}"
                        library.put(sound, target)
                        committed = True
                finally:
                    if not committed:
                        target.unlink(missing_ok=True)
                sounds.append(deepcopy(sound))
                _report(progress, index + 1, count, f"已导入 {index + 1}/{count} 条音效")
        return sounds
    except OSError:
        raise ValueError("音效包导入未完成，请检查磁盘空间和目录权限；已完成的音效已保留。") from None
    finally:
        _INSTALL_LOCK.release()
