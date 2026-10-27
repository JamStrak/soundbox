"""Index user-owned WAV ZIP packs; materialize only requested audio locally.

This registry is separate from Library. No source archive is changed, no network
request is made, and neither a ZIP member nor a browser URL chooses a cache path.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import struct
import threading
import time
import uuid
import zipfile
import zlib

from .audio import base_sound
from .search import expand_query


MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_DURATION = 600
MAX_CACHE_BYTES = 1024 * 1024 * 1024
MAX_BROWSER_PREVIEW_BYTES = 12 * 1024 * 1024  # 600 seconds of 128 kbit/s MP3 plus headers.
AUDIO_LEASE = 3600
MAX_MEMBERS = 10000
MAX_ARCHIVES = 100
MAX_HEADER_BYTES = 2 * 1024 * 1024
SONNISS_LICENSE_URL = "https://sonniss.com/gdc-bundle-license/"
_ID = re.compile(r"localpack-[0-9a-f]{24}\Z")
_CACHE_GROUP = re.compile(r"(localpack-[0-9a-f]{24}-[0-9a-f]{16})(\.wav|\.listen\.mp3)\Z")
_SONNISS = re.compile(r"Sonniss\.com-GDC2026-GameAudioBundle[1-5]of5\.zip\Z", re.I)
_RESERVED = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?\Z", re.I)


class LocalPackCancelled(ValueError):
    def __init__(self, summary):
        super().__init__("已取消本地音效包索引，已完成的条目已保留。")
        self.summary = summary
        self.items = summary["items"]


def _no_links(path: Path):
    """Reject symbolic links and Windows junction/reparse paths before resolving."""
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("本地音效包路径不能包含符号链接或目录联接。")


def _safe_member(member: zipfile.ZipInfo) -> bool:
    name = member.filename
    parts = name.rstrip("/").split("/")
    mode = stat.S_IFMT(member.external_attr >> 16)
    return bool(name and len(name) <= 4096 and name == member.orig_filename and not name.startswith("/")
                and "\\" not in name and all(
                    part not in {"", ".", ".."} and ":" not in part
                    and part.rstrip(" .") == part and not _RESERVED.fullmatch(part)
                    and not any(ord(char) < 32 or ord(char) == 127 for char in part)
                    for part in parts)
                and mode in {0, stat.S_IFREG, stat.S_IFDIR}
                and not member.flag_bits & 1
                and member.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})


def _identifier(path: Path, member: str) -> str:
    return "localpack-" + hashlib.sha256((str(path) + "\n" + member).encode("utf-8")).hexdigest()[:24]


def _fingerprint(path: Path) -> tuple[int, int]:
    _no_links(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or path.suffix.lower() != ".zip":
        raise ValueError("原音效 ZIP 已失效，请重新索引。")
    return info.st_size, info.st_mtime_ns


def _wav_info(stream, length: int) -> dict:
    """Read bounded RIFF chunks only; audio frames are not decoded or read."""
    used = 0

    def read(size):
        nonlocal used
        if size < 0 or used + size > MAX_HEADER_BYTES:
            raise ValueError("WAV 文件头过大，无法安全索引。")
        value = stream.read(size)
        used += len(value)
        if len(value) != size:
            raise ValueError("WAV 文件头不完整。")
        return value

    header = read(12)
    if header[:4] != b"RIFF" or header[8:] != b"WAVE":
        raise ValueError("暂仅支持标准 RIFF WAV。")
    end = struct.unpack_from("<I", header, 4)[0] + 8
    if end > length or end < 20:
        raise ValueError("WAV 声明大小与 ZIP 成员不一致。")
    fmt = None
    description = ""
    while used + 8 <= end:
        chunk = read(8)
        name, size = chunk[:4], struct.unpack_from("<I", chunk, 4)[0]
        if size > end - used:
            raise ValueError("WAV 数据块超出文件范围。")
        if name == b"data":
            if not fmt or size <= 0 or size % fmt["block_align"]:
                raise ValueError("WAV 音频数据或格式无效。")
            duration = size / fmt["byte_rate"]
            if duration > MAX_DURATION:
                raise ValueError(f"时长 {duration:.1f} 秒超过 {MAX_DURATION} 秒限制。")
            return {key: value for key, value in fmt.items() if key not in {"block_align", "byte_rate"}} | {
                "duration": round(duration, 6), "embedded_description": description,
                "size_bytes": length,
            }
        if name == b"fmt ":
            if size < 16 or size > 1024 or fmt is not None:
                raise ValueError("WAV 格式块无效。")
            value = read(size)
            encoding, channels, rate, byte_rate, align, bits = struct.unpack_from("<HHIIHH", value)
            if encoding == 0xFFFE:
                if (size < 40 or struct.unpack_from("<H", value, 16)[0] < 22
                        or value[28:40] != bytes.fromhex("00001000800000aa00389b71")):
                    raise ValueError("WAV Extensible 子格式无效。")
                encoding = struct.unpack_from("<I", value, 24)[0]
                valid_bits = struct.unpack_from("<H", value, 18)[0]
                if valid_bits and not 1 <= valid_bits <= bits:
                    raise ValueError("WAV 有效位深无效。")
            if (encoding not in {1, 3} or not 1 <= channels <= 32 or not 8000 <= rate <= 384000
                    or bits not in ({8, 16, 24, 32} if encoding == 1 else {32, 64})
                    or align != channels * (bits // 8) or byte_rate != rate * align):
                raise ValueError("WAV 编码、采样率或通道参数不支持。")
            fmt = {"sample_rate": rate, "channels": channels, "bit_depth": bits,
                   "codec": ("pcm_f" if encoding == 3 else "pcm_u" if bits == 8 else "pcm_s")
                            + str(bits) + ("le" if bits != 8 else ""),
                   "block_align": align, "byte_rate": byte_rate}
        elif name == b"bext" and size >= 256:
            description = read(256).split(b"\0", 1)[0].decode("utf-8", errors="replace")
            read(size - 256)
        else:
            read(size)
        if size & 1:
            read(1)
    raise ValueError("WAV 缺少可读取的音频数据块。")


def _metadata(path, member, info, licensed):
    folder = PurePosixPath(member.filename).parent
    pack_name = str(folder).split("/")[0] if str(folder) != "." else path.stem
    stem = PurePosixPath(member.filename).stem
    words = re.sub(r"([a-z])([A-Z])", r"\1 \2", stem + " " + pack_name)
    words = re.sub(r"[_.,\-]+", " ", words)
    tags = list(dict.fromkeys([*re.findall(r"[A-Za-z][A-Za-z]+", words), *expand_query(words)["terms"]]))[:40]
    sound = base_sound(_identifier(path, member.filename), stem.replace("_", " "), "localpack", "wav")
    sound.update({key: value for key, value in info.items() if key != "embedded_description"})
    sound.update({"saved": False, "downloaded": False, "source_id": sound["id"],
                  "source_file": member.filename,
                  "pack_id": "local-" + hashlib.sha256((str(path) + "\n" + pack_name).encode()).hexdigest()[:16],
                  "pack_name": pack_name, "archive_name": path.name, "tags": tags,
                  "author": pack_name.split(" - ")[0], "origin_source": "localpack", "quality": "original",
                  "preview_url": f"/api/local-packs/audio/{sound['id']}",
                  "license": "Sonniss GDC 项目使用许可" if licensed else "待确认",
                  "license_url": SONNISS_LICENSE_URL if licensed else "",
                  "source_url": "https://gdc.sonniss.com/" if licensed else "",
                  "attribution": (f"{pack_name} / {stem}；用户自行下载，仅在本人本机检索与制作项目中使用。"
                                  + ("按实际下载日 Sonniss 官网许可，未经许可不得独立再分发音效。" if licensed
                                     else "需自行核实原音效包许可，不代表开放或可再分发授权。")),
                  "description": (info.get("embedded_description") or f"{pack_name} · {stem}")
                                 + "。本地音效包原始文件，索引不代表主观音质评价。"})
    return sound


class LocalPacks:
    def __init__(self, data_dir: Path):
        self.directory = Path(data_dir).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "local-packs.sqlite3"
        self.cache = self.directory / "local-pack-cache"
        self.cache.mkdir(exist_ok=True)
        _no_links(self.path)
        _no_links(self.cache)
        self.lock = threading.RLock()
        self._leases = Counter()
        self._playback_leases = {}
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sources (path TEXT PRIMARY KEY, kind TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS local_sounds (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL, root TEXT NOT NULL,
                    archive TEXT NOT NULL, archive_size INTEGER NOT NULL, archive_mtime INTEGER NOT NULL,
                    member TEXT NOT NULL, member_size INTEGER NOT NULL, member_crc INTEGER NOT NULL
                );
            """)

    @contextmanager
    def connection(self):
        _no_links(self.path)
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _record(self, row, sources):
        """Validate persisted paths and identity before trusting registry entries."""
        try:
            archive = Path(row["archive"])
            root = Path(row["root"])
            kind = sources.get(str(root))
            if (not archive.is_absolute() or not root.is_absolute() or archive.suffix.lower() != ".zip"
                    or (kind == "file" and archive != root)
                    or (kind == "directory" and archive.parent != root)
                    or kind not in {"file", "directory"}
                    or str(archive) != os.path.normpath(str(archive))
                    or str(root) != os.path.normpath(str(root))
                    or not _ID.fullmatch(row["id"]) or row["id"] != _identifier(archive, row["member"])
                    or not 0 < row["member_size"] <= MAX_MEMBER_BYTES
                    or not 0 <= row["member_crc"] <= 0xFFFFFFFF
                    or row["archive_size"] <= 0 or row["archive_mtime"] < 0
                    or len(row["data"]) > 131072):
                raise ValueError
            member = zipfile.ZipInfo(row["member"])
            if (member.filename != row["member"] or not _safe_member(member)
                    or not row["member"].lower().endswith(".wav")):
                raise ValueError
            sound = json.loads(row["data"])
            if (not isinstance(sound, dict) or sound.get("id") != row["id"] or sound.get("source") != "localpack"
                    or sound.get("source_id") != row["id"] or sound.get("source_file") != row["member"]
                    or sound.get("format") != "wav" or not isinstance(sound.get("duration"), (int, float))
                    or not 0 < sound["duration"] <= MAX_DURATION):
                raise ValueError
            return dict(row), sound
        except (TypeError, KeyError, ValueError, json.JSONDecodeError):
            raise ValueError("本地音效包索引记录无效，请重新索引。") from None

    def _row(self, identifier):
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            raise ValueError("本地音效编号无效。")
        with self.connection() as db:
            row = db.execute("SELECT * FROM local_sounds WHERE id=?", (identifier,)).fetchone()
            sources = {item["path"]: item["kind"] for item in db.execute("SELECT * FROM sources")}
        if row is None:
            raise ValueError("本地音效不在索引中，请重新索引。")
        return self._record(row, sources)

    def all(self) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM local_sounds ORDER BY rowid").fetchall()
            sources = {item["path"]: item["kind"] for item in db.execute("SELECT * FROM sources")}
        return [self._record(row, sources)[1] for row in rows]

    def detail(self, identifier) -> dict:
        return self._row(identifier)[1]

    def index_directory(self, path: str, progress=None, cancelled=None) -> dict:
        requested = Path(path).expanduser().absolute()
        _no_links(requested)
        root = requested.resolve(strict=True)
        if root.is_dir():
            kind = "directory"
            paths = sorted((item for item in root.iterdir() if item.suffix.lower() == ".zip"), key=lambda p: p.name.casefold())
        elif root.is_file() and root.suffix.lower() == ".zip":
            kind, paths = "file", [root]
        else:
            raise ValueError("请选择本地音效包目录或一个 ZIP 文件。")
        if len(paths) > MAX_ARCHIVES:
            raise ValueError(f"一次最多索引 {MAX_ARCHIVES} 个 ZIP，请选择较小的目录。")
        summary = {"items": [], "archives": 0, "count": 0, "skipped": []}

        def checkpoint():
            if cancelled is not None and cancelled():
                raise LocalPackCancelled(summary)

        def report(completed, total, message):
            if progress is not None:
                progress(completed, total, message)

        with self.lock:
            checkpoint()
            with self.connection() as db:
                db.execute("INSERT OR REPLACE INTO sources(path,kind) VALUES (?,?)", (str(root), kind))
            for archive_path in paths:
                checkpoint()
                try:
                    fingerprint = _fingerprint(archive_path)
                    with zipfile.ZipFile(archive_path) as archive:
                        members = archive.infolist()
                        if len(members) > MAX_MEMBERS:
                            raise ValueError("ZIP 成员数量超过限制。")
                        names = Counter(member.filename.casefold() for member in members)
                        audio, license_present = [], False
                        for member in members:
                            checkpoint()
                            folded = member.filename.casefold()
                            if not _safe_member(member) or names[folded] > 1:
                                if member.filename.lower().endswith(".wav"):
                                    summary["skipped"].append(f"{archive_path.name} / {member.filename}：不安全或重复的成员。")
                                continue
                            if not member.is_dir() and re.search(r"licen[cs](?:e|ing)", member.filename, re.I):
                                license_present = True
                            if not member.is_dir() and member.filename.lower().endswith(".wav"):
                                audio.append(member)
                        licensed = bool(_SONNISS.fullmatch(archive_path.name) and license_present)
                        completed_ids = []
                        for number, member in enumerate(audio):
                            checkpoint()
                            report(number, len(audio), f"正在索引 {archive_path.name}：{member.filename}")
                            try:
                                if not 0 < member.file_size <= MAX_MEMBER_BYTES:
                                    raise ValueError("单条大小超过 512 MiB 或为空。")
                                with archive.open(member) as stream:
                                    info = _wav_info(stream, member.file_size)
                                if _fingerprint(archive_path) != fingerprint:
                                    raise ValueError("原 ZIP 已改变，请重新索引。")
                                sound = _metadata(archive_path, member, info, licensed)
                                with self.connection() as db:
                                    db.execute("INSERT OR REPLACE INTO local_sounds VALUES (?,?,?,?,?,?,?,?,?)", (
                                        sound["id"], json.dumps(sound, ensure_ascii=False), str(root), str(archive_path),
                                        *fingerprint, member.filename, member.file_size, member.CRC))
                                completed_ids.append(sound["id"])
                                summary["items"].append(sound)
                                summary["count"] += 1
                            except (ValueError, OSError, zipfile.BadZipFile, RuntimeError, EOFError, zlib.error) as error:
                                summary["skipped"].append(f"{archive_path.name} / {member.filename}：{error}")
                            report(number + 1, len(audio), f"已索引 {number + 1}/{len(audio)} 条；音频按需读取。")
                        checkpoint()
                        if _fingerprint(archive_path) != fingerprint:
                            raise ValueError("原 ZIP 已改变，请重新索引。")
                        with self.connection() as db:
                            prior = db.execute("SELECT id FROM local_sounds WHERE archive=?", (str(archive_path),)).fetchall()
                            db.executemany("DELETE FROM local_sounds WHERE id=?", [(row["id"],) for row in prior
                                                                                if row["id"] not in completed_ids])
                    summary["archives"] += 1
                except LocalPackCancelled:
                    raise
                except (ValueError, OSError, zipfile.BadZipFile, RuntimeError, EOFError, zlib.error) as error:
                    summary["skipped"].append(f"{archive_path.name}：{error}")
            report(summary["count"], summary["count"], f"索引完成：{summary['count']} 条，未复制全部音频。")
        return summary

    def _cache_path(self, row):
        _no_links(self.cache)
        stamp = f"{row['archive_size']}:{row['archive_mtime']}:{row['member_crc']}"
        token = hashlib.sha256(stamp.encode()).hexdigest()[:16]
        return self.cache / f"{row['id']}-{token}.wav"

    def _current(self, row):
        try:
            if _fingerprint(Path(row["archive"])) != (row["archive_size"], row["archive_mtime"]):
                raise ValueError
        except (ValueError, OSError):
            raise ValueError("原音效 ZIP 已移动、删除或改变，请重新索引。") from None

    def _existing(self, row):
        target = self._cache_path(row)
        _no_links(target)
        if not target.is_file():
            return None
        if target.stat().st_size != row["member_size"]:
            self._discard_group(target)
            return None
        crc = 0
        with target.open("rb") as cached:
            for block in iter(lambda: cached.read(65536), b""):
                crc = zlib.crc32(block, crc)
        if crc & 0xFFFFFFFF != row["member_crc"]:
            self._discard_group(target)
            return None
        self._touch(target)
        return target

    def _discard_group(self, target):
        for path in (target, target.with_name(target.stem + ".listen.mp3")):
            _no_links(path)
            if path.resolve().parent != self.cache.resolve():
                raise ValueError("本地缓存路径无效。")
            path.unlink(missing_ok=True)
        self._playback_leases.pop(target.stem, None)

    def _touch(self, path):
        os.utime(path, None)
        self._playback_leases[path.stem] = time.monotonic() + AUDIO_LEASE
        companion = path.with_name(path.stem + ".listen.mp3")
        _no_links(companion)
        if companion.is_file():
            os.utime(companion, None)

    def cached_file(self, identifier) -> Path | None:
        with self.lock:
            row, _ = self._row(identifier)
            self._current(row)
            return self._existing(row)

    def _prune(self, required):
        _no_links(self.cache)
        moment = time.monotonic()
        self._playback_leases = {group: until for group, until in self._playback_leases.items() if until > moment}
        groups = {}
        for path in self.cache.iterdir():
            match = _CACHE_GROUP.fullmatch(path.name)
            if match:
                _no_links(path)
                if path.resolve().parent != self.cache.resolve():
                    raise ValueError("本地缓存路径无效。")
                if path.is_file():
                    groups.setdefault(match[1], []).append((path, path.stat()))
        total = sum(info.st_size for files in groups.values() for _, info in files)
        # A leased source without a finished MP3 reserves the maximum compatible
        # preview size, preventing parallel transcodes from exceeding the budget.
        reserved = sum(MAX_BROWSER_PREVIEW_BYTES for group, count in self._leases.items()
                       if count and not any(path.name.endswith(".listen.mp3") for path, _ in groups.get(group, [])))
        for group, files in sorted(groups.items(), key=lambda item: max(info.st_mtime_ns for _, info in item[1])):
            if total + required + reserved <= MAX_CACHE_BYTES:
                break
            if self._leases[group] or group in self._playback_leases:
                continue
            for path, info in files:
                # A Windows open file may still be used by an in-flight response.
                # Keep it and continue; fail with an actionable capacity message
                # if no other unleased group can provide the required space.
                try:
                    path.unlink()
                except PermissionError:
                    continue
                total -= info.st_size
        if total + required + reserved > MAX_CACHE_BYTES:
            raise ValueError("本地音效缓存空间不足；正在试听或使用的音频会暂时保留，请稍后重试。")

    @contextmanager
    def lease_file(self, identifier):
        """Protect the source and its browser preview until the caller releases it.

        HTTP callers can retain this context through FileResponse's background
        cleanup; FFmpeg/copy callers use a normal with-block. No cache file gets
        removed while this context is live.
        """
        with self.lock:
            path = self.audio_file(identifier)
            group = path.stem
            self._leases[group] += 1
            try:
                self._prune(0)
            except Exception:
                self._leases[group] -= 1
                raise
        try:
            yield path
        finally:
            with self.lock:
                self._leases[group] -= 1
                if self._leases[group] <= 0:
                    del self._leases[group]
                self._prune(0)

    def audio_file(self, identifier) -> Path:
        with self.lock:
            row, _ = self._row(identifier)
            self._current(row)
            cached = self._existing(row)
            if cached is not None:
                return cached
            self._prune(row["member_size"])
            target = self._cache_path(row)
            temporary = self.cache / (".localpack-" + uuid.uuid4().hex + ".part")
            try:
                with zipfile.ZipFile(row["archive"]) as archive:
                    members = [item for item in archive.infolist() if item.filename == row["member"]]
                    if len(members) != 1:
                        raise ValueError("原 ZIP 成员已改变，请重新索引。")
                    member = members[0]
                    if (not _safe_member(member) or member.file_size != row["member_size"]
                            or member.CRC != row["member_crc"]):
                        raise ValueError("原 ZIP 成员校验不一致，请重新索引。")
                    written, crc = 0, 0
                    with archive.open(member) as source, temporary.open("xb") as output:
                        for block in iter(lambda: source.read(65536), b""):
                            written += len(block)
                            if written > row["member_size"] or written > MAX_MEMBER_BYTES:
                                raise ValueError("音频大小超出已索引范围。")
                            crc = zlib.crc32(block, crc)
                            output.write(block)
                        output.flush()
                        os.fsync(output.fileno())
                    if written != row["member_size"] or crc & 0xFFFFFFFF != row["member_crc"]:
                        raise ValueError("音频 CRC 或大小校验失败。")
                self._current(row)
                _no_links(target)
                os.replace(temporary, target)
                self._touch(target)
                return target
            except (OSError, zipfile.BadZipFile, RuntimeError, EOFError, zlib.error) as error:
                raise ValueError("本地音频读取失败，ZIP 可能损坏；请重新索引。") from error
            finally:
                if temporary.exists():
                    temporary.unlink()

    def download(self, identifier) -> tuple[bytes, str]:
        return self.audio_file(identifier).read_bytes(), "wav"
