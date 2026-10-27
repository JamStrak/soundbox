"""Searchable, pinned open game-audio catalog with disposable on-demand audio.

Search is local metadata only. An explicit first listen fetches the fixed official
archive, verifies its exact version and extracts one hashed member into a cache;
it does not create personal-library entries or accept URLs from the browser.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import threading
import time
from urllib.parse import urlsplit
import uuid
import zipfile
import zlib

import httpx

from . import providers
from .audio import base_sound
from .search import local_match

MAX_ARCHIVE_BYTES = 192 * 1024 * 1024
MAX_MEMBER_BYTES = 50 * 1024 * 1024
MAX_UNPACKED_BYTES = 768 * 1024 * 1024
MAX_MEMBERS = 5000
CACHE_BUDGET = 512 * 1024 * 1024
CACHE_LIFETIME = 7 * 86400
# An audio request lasts at most 600 seconds; a generous lease keeps its path
# available to FileResponse and the player while older disposable files retire.
AUDIO_LEASE = 3600
PAGE_SIZE = 40
_HASH = re.compile(r"[0-9a-f]{64}")
_PACK_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,59}")
_CACHE_NAME = re.compile(r"(?:catalog-[a-z0-9-]+-[0-9a-f]{12}-[0-9a-f]{64}\.(?:wav|ogg|mp3|flac|m4a|aac|listen\.mp3)|archive-[a-z0-9-]+-[0-9a-f]{64}\.zip)")
_FORMATS = {"wav", "ogg", "mp3", "flac", "m4a", "aac"}


def identifier(pack: dict, entry: dict) -> str:
    return "catalog-" + pack["id"] + "-" + hashlib.sha256(entry["path"].encode("utf-8")).hexdigest()[:12]


def _checkpoint(cancelled):
    if cancelled is not None and cancelled():
        raise ValueError("已取消开放音效下载。")


def _safe_member(name: str) -> bool:
    components = name.rstrip("/").split("/")
    return bool(name and not name.startswith("/") and "\\" not in name
                and all(part not in {"", ".", ".."} and ":" not in part
                        and part.rstrip(" .") == part and not any(ord(c) < 32 for c in part)
                        for part in components))


def _safe_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        path = parts.path
        valid = (parts.scheme == "https" and parts.port in (None, 443)
                 and not parts.username and not parts.password and not parts.query and not parts.fragment
                 and "\\" not in url and ".." not in path and path.endswith(".zip")
                 and ((parts.hostname == "kenney.nl" and path.startswith("/media/pages/assets/"))
                      or (parts.hostname == "opengameart.org" and path.startswith("/sites/default/files/"))))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("开放音效包地址未经核实，已停止下载。")
    return url


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Catalog:
    def __init__(self, directory: Path, manifest: dict | None = None):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        candidate = self.directory / "catalog-cache"
        candidate.mkdir(exist_ok=True)
        self.cache = candidate.resolve()
        if self.cache.parent != self.directory:
            raise ValueError("开放音效缓存目录不在当前资料目录内。")
        self.lock = threading.RLock()
        self.leases: dict[Path, float] = {}
        if manifest is None:
            manifest = json.loads(Path(__file__).with_name("catalog_manifest.json").read_text(encoding="utf-8"))
        self.manifest = deepcopy(manifest)
        self.entries: dict[str, tuple[dict, dict]] = {}
        self.pack_index: dict[str, dict] = {}
        self._validate()
        # Interrupted downloads belong to this disposable cache. Keep recent
        # staging files conservative, and retire old abandoned ones on startup.
        for path in self.cache.iterdir():
            if (not path.is_symlink() and path.is_file()
                    and re.fullmatch(r"\.(download|extract)-[0-9a-f]{32}\.tmp", path.name)
                    and time.time() - path.stat().st_mtime > AUDIO_LEASE):
                try:
                    path.unlink()
                except OSError:
                    pass

    def _validate(self):
        if self.manifest.get("version") != 1 or not isinstance(self.manifest.get("packs"), list):
            raise ValueError("开放音效清单版本不受支持，请更新声匣。")
        for pack in self.manifest["packs"]:
            if (not isinstance(pack, dict) or not _PACK_ID.fullmatch(str(pack.get("id", "")))
                    or pack["id"] in self.pack_index or not _HASH.fullmatch(str(pack.get("sha256", "")))
                    or type(pack.get("bytes")) is not int or not 0 < pack["bytes"] <= MAX_ARCHIVE_BYTES
                    or pack.get("license") != "CC0"
                    or pack.get("license_url") != "https://creativecommons.org/publicdomain/zero/1.0/"
                    or not pack.get("license_evidence_url") or not pack.get("source_url")
                    or not pack.get("author") or not pack.get("name")
                    or not isinstance(pack.get("files"), list) or not 0 < len(pack["files"]) <= MAX_MEMBERS):
                raise ValueError("开放音效清单缺少已核实的来源、许可或文件信息。")
            _safe_url(pack.get("download_url", ""))
            seen = set()
            for entry in pack["files"]:
                if not isinstance(entry, dict):
                    raise ValueError("开放音效条目格式无效。")
                path = entry.get("path", "")
                fmt = str(entry.get("format", "")).lower()
                duration = entry.get("duration")
                if (not isinstance(path, str) or not _safe_member(path) or path.casefold() in seen
                        or fmt not in _FORMATS or PurePosixPath(path).suffix.lower() != "." + fmt
                        or not _HASH.fullmatch(str(entry.get("sha256", "")))
                        or type(entry.get("bytes")) is not int or not 0 < entry["bytes"] <= MAX_MEMBER_BYTES
                        or not isinstance(duration, (int, float)) or isinstance(duration, bool)
                        or not math.isfinite(duration) or not 0 < duration <= 600
                        or not entry.get("name") or not isinstance(entry.get("tags"), list)
                        or any(not isinstance(tag, str) for tag in entry["tags"])):
                    raise ValueError("开放音效条目缺少已核实的名称、格式、时长或哈希。")
                seen.add(path.casefold())
                sound_id = identifier(pack, entry)
                if sound_id in self.entries:
                    raise ValueError("开放音效清单包含重复编号，未加载。")
                self.entries[sound_id] = (pack, entry)
            self.pack_index[pack["id"]] = pack

    def _entry(self, sound_id: str) -> tuple[dict, dict]:
        if not isinstance(sound_id, str) or sound_id not in self.entries:
            raise ValueError("开放音效编号不存在，请重新搜索。")
        return self.entries[sound_id]

    def packs(self) -> list[dict]:
        return [{"id": p["id"], "name": p["name"], "description": p.get("description", ""),
                 "author": p["author"], "license": p["license"], "license_url": p["license_url"],
                 "count": len(p["files"]), "bytes": p["bytes"], "source_url": p["source_url"],
                 "origin_source": self._origin(p)} for p in self.pack_index.values()]

    @staticmethod
    def _origin(pack):
        return pack.get("origin_source") or ("kenney" if urlsplit(pack["download_url"]).hostname == "kenney.nl" else "opengameart")

    def detail(self, sound_id: str) -> dict:
        pack, entry = self._entry(sound_id)
        sound = base_sound(sound_id, entry["name"], "catalog", entry["format"])
        sound.update({"source_id": sound_id, "origin_source": self._origin(pack), "saved": False,
                      "downloaded": False, "preview_url": "/api/catalog/audio/" + sound_id,
                      "tags": deepcopy(entry["tags"]), "duration": entry["duration"], "waveform": [],
                      "description": entry.get("description") or f"原始文件：{entry['path']}。名称与标签来自作者目录，适合与否需试听。",
                      "pack_id": pack["id"], "pack_name": pack["name"], "catalog_pack_bytes": pack["bytes"],
                      "author": pack["author"], "license": pack["license"], "license_url": pack["license_url"],
                      "license_evidence_url": pack["license_evidence_url"], "source_url": pack["source_url"],
                      "download_url": pack["download_url"], "source_file": entry["path"],
                      "content_sha256": entry["sha256"], "sample_rate": entry.get("sample_rate"),
                      "channels": entry.get("channels"), "codec": entry.get("codec"),
                      "download_label": "官方原始音频", "quality": "original",
                      "attribution": f"{pack.get('original_name') or pack['name']} / {entry['path']} by {pack['author']} — {pack['license']}. {pack['source_url']}"})
        if entry.get("variant_group"):
            sound["variant_group"] = "catalog-" + pack["id"] + "-" + entry["variant_group"]
        return sound

    def search(self, query: dict, page=1, license="all", duration_max=0, sort="score", pack_id="") -> dict:
        if pack_id and pack_id not in self.pack_index:
            raise ValueError("开放音效包不存在，请刷新来源列表。")
        if license not in {"all", "commercial", "cc0"}:
            raise ValueError("音效许可筛选无效。")
        search_text = query.get("translated") or query.get("original") or ""
        sounds = []
        for sound_id, (pack, entry) in self.entries.items():
            if pack_id and pack["id"] != pack_id:
                continue
            if duration_max and entry["duration"] > duration_max:
                continue
            # Preserve every required concept, rather than broad OR matches that
            # would fill the list with unrelated sounds from the same pack.
            text = " ".join([entry["name"], " ".join(entry["tags"]), entry.get("description", "")])
            if not local_match(search_text, text):
                continue
            sounds.append(self.detail(sound_id))
        if sort in {"duration", "duration_asc"}:
            sounds.sort(key=lambda s: (s["duration"], s["name"]))
        elif sort == "duration_desc":
            sounds.sort(key=lambda s: (-s["duration"], s["name"]))
        elif sort == "name":
            sounds.sort(key=lambda s: s["name"].casefold())
        elif sort == "score" and search_text:
            sounds.sort(key=lambda s: not local_match(search_text, s["name"]))
        total = len(sounds)
        offset = (max(1, int(page)) - 1) * PAGE_SIZE
        result = {"items": sounds[offset:offset + PAGE_SIZE], "total": total, "page": max(1, int(page)),
                  "has_more": offset + PAGE_SIZE < total, "query": deepcopy(query),
                  "pagination_note": "开放游戏音效目录按完整清单检索；首次试听按需下载所在官方音效包，试听缓存不会加入个人素材库。"}
        if query.get("warning"):
            result["warning"] = query["warning"]
        return result

    def _audio_path(self, sound_id, entry):
        return self.cache / (sound_id + "-" + entry["sha256"] + "." + entry["format"])

    def _archive_path(self, pack):
        return self.cache / ("archive-" + pack["id"] + "-" + pack["sha256"] + ".zip")

    def _touch(self, path, *, audio=False):
        os.utime(path, None)
        if audio:
            lease = time.time() + AUDIO_LEASE
            self.leases[path] = lease
            # The route may create this browser-compatible companion after
            # audio_file returns; lease the owned name before it exists.
            companion = path.with_name(path.stem + ".listen.mp3")
            self.leases[companion] = lease
            if companion.is_file() and not companion.is_symlink():
                os.utime(companion, None)

    def cached_file(self, sound_id: str) -> Path | None:
        with self.lock:
            _, entry = self._entry(sound_id)
            path = self._audio_path(sound_id, entry)
            if self._valid_file(path, entry["bytes"], entry["sha256"]):
                self._touch(path, audio=True)
                return path
            return None

    def _valid_file(self, path, size, digest):
        # Never follow a tampered cache symlink outside the disposable directory.
        if path.is_symlink() or path.resolve().parent != self.cache:
            raise ValueError("开放音效缓存路径异常，请移除缓存链接后重试。")
        try:
            if path.is_file() and path.stat().st_size == size and _hash_file(path) == digest:
                return True
            if path.is_file():
                path.unlink()
        except OSError:
            raise ValueError("无法读取开放音效缓存，请检查目录权限后重试。") from None
        return False

    def _prune(self, reserve=0, exclude=()):
        moment = time.time()
        self.leases = {p: until for p, until in self.leases.items() if until > moment}
        paths = [p for p in self.cache.iterdir() if p.is_file() and not p.is_symlink() and _CACHE_NAME.fullmatch(p.name)]
        total = sum(p.stat().st_size for p in paths)
        groups = {}
        for path in paths:
            # Originals and their .listen.mp3 are retired together, so a
            # conversion can neither leak budget nor outlive its source.
            key = path.name.removesuffix(".listen.mp3") if path.name.endswith(".listen.mp3") else path.stem
            groups.setdefault(key, []).append(path)
        for group in sorted(groups.values(), key=lambda value: min(p.stat().st_mtime for p in value)):
            if any(path in exclude or path in self.leases for path in group):
                continue
            modified = max(path.stat().st_mtime for path in group)
            if total + reserve <= CACHE_BUDGET and moment - modified < CACHE_LIFETIME:
                continue
            for path in group:
                try:
                    size = path.stat().st_size
                    path.unlink()
                    total -= size
                except OSError:
                    pass  # A Windows handle may still be finishing an old response.
        if total + reserve > CACHE_BUDGET:
            raise providers.ProviderError("开放音效试听缓存暂时已满，请结束当前试听后稍后重试。", 409)

    def _download_archive(self, pack, target, max_download_bytes, on_bytes, cancelled):
        expected = pack["bytes"]
        if max_download_bytes is not None and expected > max_download_bytes:
            raise ValueError("首次试听需下载整个官方音效包，超过本次下载预算；请先单独试听这条素材。")
        temporary = self.cache / (".download-" + uuid.uuid4().hex + ".tmp")
        digest, received = hashlib.sha256(), 0
        try:
            _checkpoint(cancelled)
            with providers._client() as client:
                with client.stream("GET", _safe_url(pack["download_url"]), headers={"Accept": "application/zip"}) as response:
                    providers._status_error(response, "开放音效官方下载")
                    length = response.headers.get("content-length")
                    if length is not None and (not length.isdigit() or int(length) != expected):
                        raise ValueError("音效包大小与已核实版本不同，请等待清单更新。")
                    with temporary.open("xb") as output:
                        for chunk in response.iter_bytes(65536):
                            _checkpoint(cancelled)
                            received += len(chunk)
                            if on_bytes is not None:
                                on_bytes(len(chunk))
                            if received > expected:
                                raise ValueError("音效包超过已核实体积，已停止下载。")
                            digest.update(chunk)
                            output.write(chunk)
            _checkpoint(cancelled)
            if received != expected or digest.hexdigest() != pack["sha256"]:
                raise ValueError("音效包哈希校验失败，官方版本可能已变化；没有提取音频。")
            os.replace(temporary, target)
        except httpx.HTTPError as error:
            raise providers._network_error(error, "开放音效官方下载") from None
        except OSError:
            raise ValueError("开放音效包缓存未完成，请检查磁盘空间与目录权限后重试。") from None
        finally:
            temporary.unlink(missing_ok=True)

    def _extract(self, archive_path, sound_id, pack, entry, cancelled):
        target = self._audio_path(sound_id, entry)
        temporary = self.cache / (".extract-" + uuid.uuid4().hex + ".tmp")
        try:
            with zipfile.ZipFile(archive_path) as archive:
                members = archive.infolist()
                if len(members) > MAX_MEMBERS:
                    raise ValueError("音效包成员数量超出安全限制。")
                by_name, total = {}, 0
                for member in members:
                    _checkpoint(cancelled)
                    name = member.filename
                    mode = stat.S_IFMT(member.external_attr >> 16)
                    if (member.orig_filename != name or not _safe_member(name) or name.casefold() in by_name
                            or mode not in {0, stat.S_IFREG, stat.S_IFDIR} or member.flag_bits & 1
                            or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
                        raise ValueError("音效包包含不安全路径、链接、加密或重复成员；没有提取音频。")
                    total += member.file_size
                    if member.file_size > MAX_MEMBER_BYTES or total > MAX_UNPACKED_BYTES:
                        raise ValueError("音效包解压体积超出安全限制。")
                    by_name[name.casefold()] = member
                for expected in pack["files"]:
                    member = by_name.get(expected["path"].casefold())
                    if member is None or member.filename != expected["path"] or member.is_dir() or member.file_size != expected["bytes"]:
                        raise ValueError("音效包音频目录与已核实清单不同；没有提取音频。")
                for license_file in pack.get("license_files", []):
                    member = by_name.get(license_file["path"].casefold())
                    if member is None or member.file_size > 65536 or hashlib.sha256(archive.read(member)).hexdigest() != license_file["sha256"]:
                        raise ValueError("音效包内的许可文件与已核实版本不同。")
                member = by_name[entry["path"].casefold()]
                digest, received = hashlib.sha256(), 0
                with archive.open(member) as source, temporary.open("xb") as output:
                    while chunk := source.read(65536):
                        _checkpoint(cancelled)
                        received += len(chunk)
                        if received > entry["bytes"]:
                            raise ValueError("音频文件超出已核实体积。")
                        digest.update(chunk)
                        output.write(chunk)
                if received != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
                    raise ValueError("音效文件校验失败，没有提供试听。")
                _checkpoint(cancelled)
                os.replace(temporary, target)
                self._touch(target, audio=True)
                return target
        except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error):
            raise ValueError("音效包无法安全读取，文件可能损坏；没有提取音频。") from None
        finally:
            temporary.unlink(missing_ok=True)

    def audio_file(self, sound_id: str, max_download_bytes=None, on_bytes=None, cancelled=None, *, allow_download=True) -> Path:
        with self.lock:
            _checkpoint(cancelled)
            cached = self.cached_file(sound_id)
            if cached is not None:
                return cached
            if not allow_download:
                raise providers.ProviderError("请先试听这条素材，完成官方音效包的缓存后再检查音频。", 400)
            pack, entry = self._entry(sound_id)
            archive = self._archive_path(pack)
            exists = self._valid_file(archive, pack["bytes"], pack["sha256"])
            reserve = entry["bytes"] + (0 if exists else pack["bytes"])
            self._prune(reserve, exclude=(archive,))
            if not exists:
                self._download_archive(pack, archive, max_download_bytes, on_bytes, cancelled)
            self._touch(archive)
            try:
                return self._extract(archive, sound_id, pack, entry, cancelled)
            except OSError:
                raise ValueError("开放音效缓存未完成，请检查磁盘空间与目录权限后重试。") from None

    def download(self, sound_id: str, max_download_bytes=None, on_bytes=None, cancelled=None, *, allow_download=True) -> tuple[bytes, str]:
        with self.lock:
            path = self.audio_file(sound_id, max_download_bytes, on_bytes, cancelled, allow_download=allow_download)
            _checkpoint(cancelled)
            return path.read_bytes(), path.suffix.lstrip(".")
