"""Bounded local video-to-audio drafts, separate from the personal library."""
from __future__ import annotations

import array
from contextlib import contextmanager
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import wave

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.concurrency import run_in_threadpool
from python_multipart.exceptions import MultipartParseError

from .audio import base_sound, waveform

MAX_VIDEO_BYTES = 512 * 1024 * 1024
MAX_VIDEO_SECONDS = 600
DRAFT_TTL_SECONDS = 3600
MAX_DRAFTS = 8
MAX_CACHE_BYTES = 512 * 1024 * 1024
MULTIPART_OVERHEAD = 1024 * 1024
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.m4v', '.webm', '.mkv', '.avi'}
IDENTIFIER = re.compile(r'[0-9a-f]{32}')
TEMP_FILE = re.compile(r'[0-9a-f]{32}(?:\.wav|\.upload\.(?:mp4|mov|m4v|webm|mkv|avi))')


def video_upload_path(path: str) -> bool:
    return bool(re.fullmatch(r'/api/video-sources/[0-9a-f]{32}', path))


def _identifier(value: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise HTTPException(422, '视频草稿标识无效，请重新选择视频。')
    return value


def _run(command: list[str], timeout: float, cancelled) -> bytes:
    """Stop our own decoder promptly on cancellation; never invoke a shell."""
    if cancelled():
        raise HTTPException(409, '视频提取已取消，请重新选择视频。')
    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        deadline = time.monotonic() + timeout
        while True:
            if cancelled():
                raise HTTPException(409, '视频提取已取消，请重新选择视频。')
            if time.monotonic() >= deadline:
                raise ValueError('视频处理超时，请选择较短的视频，或先将视频导出为 MP4 后重试。')
            try:
                output, _ = process.communicate(timeout=.25)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise ValueError('这个视频无法读取，可能已损坏或使用了不支持的编码，请先导出为 MP4 后重试。')
        return output
    except OSError as exc:
        raise ValueError('无法启动视频处理，请确认声匣的 FFmpeg 组件可用。') from exc
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()


def _input_options(path: Path) -> list[str]:
    # Force a real container parser: renamed playlists cannot read local files.
    # MOV external data references remain disabled, including absolute paths.
    demuxer = 'mov' if path.suffix in {'.mp4', '.mov', '.m4v'} else 'avi' if path.suffix == '.avi' else 'matroska'
    options = ['-protocol_whitelist', 'file,pipe', '-format_whitelist', 'mov,matroska,webm,avi',
               '-f', demuxer, '-probesize', '5000000', '-analyzeduration', '5000000']
    if demuxer == 'mov':
        options.extend(['-enable_drefs', '0', '-use_absolute_path', '0'])
    return options


def extract_audio(source: Path, target: Path, cancelled) -> dict:
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        raise ValueError('提取视频音轨需要 FFmpeg。请使用包含运行组件的声匣版本，或安装 FFmpeg 后重试。')
    try:
        metadata = json.loads(_run([
            ffprobe, '-hide_banner', '-v', 'error', *_input_options(source),
            '-show_entries', 'format=duration:stream=codec_type,duration:stream_disposition=attached_pic',
            '-of', 'json', str(source),
        ], 40, cancelled))
    except json.JSONDecodeError as exc:
        raise ValueError('无法识别视频的音轨信息，请将视频导出为 MP4 后重试。') from exc
    streams = metadata.get('streams', [])
    if not any(s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic') for s in streams):
        raise ValueError('这个文件没有视频画面；音频文件请使用“导入音效”。')
    audio = [s for s in streams if s.get('codec_type') == 'audio']
    if not audio:
        raise ValueError('这个视频没有音轨，请选择包含声音的视频。')
    duration = audio[0].get('duration') or metadata.get('format', {}).get('duration')
    if duration is not None:
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            duration = None
        if duration is not None and (not math.isfinite(duration) or duration <= 0 or duration > MAX_VIDEO_SECONDS + .001):
            raise ValueError('视频音轨需在 10 分钟以内，请先截短视频后再提取。')
    # Decode slightly beyond the limit so a lying/missing duration cannot be
    # silently truncated into an apparently complete draft.
    _run([ffmpeg, '-nostdin', '-hide_banner', '-v', 'error', '-y', *_input_options(source),
          '-i', str(source), '-map', '0:a:0', '-vn', '-sn', '-dn', '-map_metadata', '-1',
          '-t', str(MAX_VIDEO_SECONDS + .02), '-ac', '2', '-ar', '44100',
          '-c:a', 'pcm_s16le', '-f', 'wav', str(target)], 180, cancelled)
    try:
        with wave.open(str(target), 'rb') as stream:
            duration = stream.getnframes() / stream.getframerate()
            if not .02 <= duration <= MAX_VIDEO_SECONDS + .001:
                raise ValueError('音轨太短、没有声音数据或超过 10 分钟，请换一个视频后重试。')
            # Read bounded chunks, keeping only low-rate waveform samples.
            samples = array.array('h')
            while data := stream.readframes(44100):
                chunk = array.array('h', data)
                if sys.byteorder != 'little':
                    chunk.byteswap()
                samples.extend(chunk[::88])
        return {'duration': round(duration, 3), 'waveform': waveform(samples), 'audio_track_count': len(audio)}
    except (wave.Error, EOFError) as exc:
        raise ValueError('音轨未能完整提取，请重新选择视频。') from exc


class LeasedFileResponse(FileResponse):
    def __init__(self, path: Path, release):
        super().__init__(path, media_type='audio/wav')
        self.release = release

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.release()


class VideoSources:
    def __init__(self, directory: Path):
        self.directory = directory / 'video-sources'
        self.directory.mkdir(exist_ok=True)
        if self.directory.is_symlink():
            raise ValueError('视频草稿目录不能是符号链接。')
        self.entries: dict[str, dict] = {}
        self.cancelled: dict[str, float] = {}
        self.pending: set[str] = set()
        self.lock = threading.RLock()
        self.slot = threading.BoundedSemaphore(1)
        with self.lock:
            self.cleanup()

    def _remove(self, path: Path) -> bool:
        if path.parent != self.directory or path.is_symlink() or not TEMP_FILE.fullmatch(path.name):
            return False
        try:
            path.unlink(missing_ok=True)
            return True
        except PermissionError:
            return False

    def cleanup(self):
        """Caller holds lock. Active readers/renderers keep their original file."""
        expired = time.time() - DRAFT_TTL_SECONDS
        for identifier, entry in list(self.entries.items()):
            if entry['created'] < expired:
                entry['discarded'] = True
            if entry.get('discarded') and not entry['leases'] and self._remove(entry['path']):
                del self.entries[identifier]
        known = {entry['path'] for entry in self.entries.values()}
        for path in self.directory.iterdir():
            if path in known or path.name[:32] in self.pending or not TEMP_FILE.fullmatch(path.name):
                continue
            if path.is_file() and path.stat().st_mtime < expired:
                self._remove(path)
        for identifier, created in list(self.cancelled.items()):
            if created < time.time() - 3600 and identifier not in self.pending:
                del self.cancelled[identifier]

    def _cache_bytes(self):
        return sum(path.stat().st_size for path in self.directory.glob('*.wav')
                   if path.is_file() and TEMP_FILE.fullmatch(path.name))

    def begin(self, identifier: str):
        with self.lock:
            self.cleanup()
            if identifier in self.cancelled:
                raise HTTPException(409, '视频提取已取消，请重新选择视频。')
            if identifier in self.entries or identifier in self.pending:
                raise HTTPException(409, '这个视频草稿已存在，请重新选择视频。')
            if len(self.cancelled) >= 2048:
                raise HTTPException(429, '视频操作过于频繁，请稍后重试。')
            reserve = math.ceil((MAX_VIDEO_SECONDS + .02) * 44100 * 4) + 4096
            # A draft without a current reader may still be open in an editor.
            # Only evict orphaned files, never another window's live draft.
            for path in sorted(self.directory.glob('*.wav'), key=lambda p: p.stat().st_mtime):
                if len(self.entries) < MAX_DRAFTS and self._cache_bytes() + reserve <= MAX_CACHE_BYTES:
                    break
                key = path.stem
                entry = self.entries.get(key)
                if entry:
                    continue
                if self._remove(path):
                    self.entries.pop(key, None)
            if len(self.entries) >= MAX_DRAFTS or self._cache_bytes() + reserve > MAX_CACHE_BYTES:
                raise HTTPException(409, '视频草稿空间已满，请关闭其他剪辑或停止试听后重试。')
            self.pending.add(identifier)

    def is_cancelled(self, identifier: str) -> bool:
        with self.lock:
            return identifier in self.cancelled

    def discard(self, identifier: str):
        _identifier(identifier)
        with self.lock:
            self.cleanup()
            if len(self.cancelled) >= 2048 and identifier not in self.cancelled and identifier not in self.pending and identifier not in self.entries:
                raise HTTPException(429, '取消请求过于频繁，请稍后重试。')
            self.cancelled[identifier] = time.time()
            entry = self.entries.get(identifier)
            if entry:
                entry['discarded'] = True
            self.cleanup()

    def acquire(self, sound_id: str):
        identifier = _identifier(sound_id.removeprefix('video-'))
        with self.lock:
            self.cleanup()
            entry = self.entries.get(identifier)
            if not entry or entry.get('discarded') or not entry['path'].is_file():
                raise HTTPException(404, '视频音轨草稿已关闭、过期或服务已重启，请重新拖入视频。')
            entry['leases'] += 1
            return deepcopy(entry['sound']), entry['path'], identifier

    def release(self, identifier: str):
        with self.lock:
            entry = self.entries.get(identifier)
            if entry:
                entry['leases'] = max(0, entry['leases'] - 1)
            self.cleanup()

    @contextmanager
    def lease(self, sound_id: str):
        sound, path, identifier = self.acquire(sound_id)
        try:
            yield sound, path
        finally:
            self.release(identifier)

    def extract(self, identifier: str, upload: UploadFile) -> dict:
        filename = Path((upload.filename or '').replace('\\', '/')).name
        if not filename or len(filename) > 250 or any(ord(c) < 32 for c in filename):
            raise ValueError('视频文件名无效，请重命名后重试。')
        extension = Path(filename).suffix.lower()
        if extension not in VIDEO_EXTENSIONS:
            raise ValueError('请选择 MP4、MOV、M4V、WebM、MKV 或 AVI 视频。')
        source = self.directory / f'{identifier}.upload{extension}'
        target = self.directory / f'{identifier}.wav'
        if source.exists() or source.is_symlink() or target.exists() or target.is_symlink():
            raise HTTPException(409, '这个视频草稿标识已被使用，请重新选择视频。')
        try:
            total = 0
            with source.open('xb') as output:
                while chunk := upload.file.read(1024 * 1024):
                    if self.is_cancelled(identifier):
                        raise HTTPException(409, '视频提取已取消，请重新选择视频。')
                    total += len(chunk)
                    if total > MAX_VIDEO_BYTES:
                        raise HTTPException(413, '视频不能超过 512 MB，请先压缩或截短后重试。')
                    output.write(chunk)
            if not total:
                raise ValueError('视频文件为空，请重新选择视频。')
            analysis = extract_audio(source, target, lambda: self.is_cancelled(identifier))
            sound = base_sound('video-' + identifier, Path(filename).stem[:190] + ' · 视频音轨', 'video') | analysis
            sound.update(saved=False, preview_url=f'/api/video-sources/{identifier}/audio',
                         original_filename=filename, quality='extracted', origin_source='video',
                         description='从用户选择的视频中提取第一条音轨；保存剪辑后才会加入素材库。',
                         attribution=f'来源视频：{filename}；提取第一条音轨；使用许可由原视频决定，待确认。')
            with self.lock:
                if identifier in self.cancelled:
                    raise HTTPException(409, '视频提取已取消，请重新选择视频。')
                self.entries[identifier] = {'sound': sound, 'path': target, 'created': time.time(), 'leases': 0}
            return deepcopy(sound)
        finally:
            self._remove(source)
            with self.lock:
                if identifier not in self.entries:
                    self._remove(target)


def register_video(app, library) -> VideoSources:
    sources = VideoSources(library.directory)
    app.state.video_sources = sources

    @app.post('/api/video-sources/{identifier}')
    async def upload_video(identifier: str, request: Request):
        _identifier(identifier)
        if not sources.slot.acquire(blocking=False):
            raise HTTPException(409, '已有视频正在提取，请等待完成或取消后重试。')
        began = False
        try:
            sources.begin(identifier)
            began = True
            total = 0
            oversized = False

            async def receive():
                nonlocal total, oversized
                message = await request.receive()
                if message['type'] == 'http.request':
                    total += len(message.get('body', b''))
                    if total > MAX_VIDEO_BYTES + MULTIPART_OVERHEAD:
                        oversized = True
                        # MultiPartException closes every spool opened so far.
                        raise MultiPartException('视频不能超过 512 MB，请先压缩或截短后重试。')
                    if sources.is_cancelled(identifier):
                        raise MultiPartException('视频提取已取消，请重新选择视频。')
                return message

            bounded = Request(request.scope, receive=receive)
            if not request.headers.get('content-type', '').lower().startswith('multipart/form-data;'):
                raise HTTPException(422, '请通过声匣选择或拖入一个视频文件。')
            parser = MultiPartParser(bounded.headers, bounded.stream(), max_files=1, max_fields=0)
            try:
                form = await parser.parse()
                upload = form.get('file')
                if not isinstance(upload, UploadFile) or len(form) != 1:
                    raise HTTPException(422, '请每次选择一个视频文件。')
                return await run_in_threadpool(sources.extract, identifier, upload)
            except (MultiPartException, MultipartParseError):
                if oversized:
                    raise HTTPException(413, '视频不能超过 512 MB，请先压缩或截短后重试。') from None
                if sources.is_cancelled(identifier):
                    raise HTTPException(409, '视频提取已取消，请重新选择视频。') from None
                raise HTTPException(400, '视频上传格式无效，请每次选择一个视频文件。') from None
            finally:
                # Starlette normally closes spools only for MultiPartException;
                # disconnects, malformed bytes and task cancellation also need
                # cleanup, including files whose part never finished parsing.
                for spool in parser._files_to_close_on_error:
                    spool.close()
        except OSError:
            raise HTTPException(500, '无法保存视频草稿，请检查磁盘空间与文件夹权限。') from None
        finally:
            if began:
                with sources.lock:
                    sources.pending.discard(identifier)
                    sources.cleanup()
            sources.slot.release()

    @app.get('/api/video-sources/{identifier}/audio')
    def video_audio(identifier: str):
        _, path, key = sources.acquire(_identifier(identifier))
        return LeasedFileResponse(path, lambda: sources.release(key))

    @app.delete('/api/video-sources/{identifier}')
    def delete_video(identifier: str):
        sources.discard(identifier)
        return {'ok': True}

    return sources
