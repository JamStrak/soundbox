"""Local inspection jobs, reversible exclusions and non-destructive level copies."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from . import gamepacks, openverse, providers, quality
from .audio import base_sound, inspect_audio
from .editor_api import provenance
from .storage import now

FILE_BUDGET = 5 * 1024 * 1024
BATCH_BUDGET = 30 * 1024 * 1024
CACHE_BUDGET = 200 * 1024 * 1024
CHECK_VERSION = 1


def stamp(path: Path) -> str:
    stat = path.stat()
    return f'{stat.st_size}:{stat.st_mtime_ns}'


def optional_stamp(path: Path | None) -> str | None:
    try:
        return stamp(path) if path else None
    except OSError:
        return None


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class CheckRequest(Input):
    ids: list[str] = Field(min_length=1, max_length=10)


class Visibility(Input):
    hidden: bool


class SaveCompensation(Input):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    folder_id: str | None = None


def filtered(sounds: list[dict], visibility: str, audio_filter: str) -> tuple[list[dict], int]:
    output = []
    hidden_count = 0
    for sound in sounds:
        check = sound.get('audio_check') or {}
        hidden = sound.get('hidden', False) or check.get('status') == 'unusable'
        if hidden:
            hidden_count += 1
        if visibility == 'visible' and hidden:
            continue
        if visibility == 'hidden' and not hidden:
            continue
        if audio_filter == 'checked' and check.get('status') != 'ready':
            continue
        if audio_filter == 'unchecked' and check:
            continue
        if audio_filter == 'quiet' and 'quiet' not in check.get('flags', []):
            continue
        if audio_filter == 'issues' and not check.get('flags') and not sound.get('audio_check_error'):
            continue
        output.append(sound)
    return output, hidden_count


class AudioService:
    def __init__(self, app, library, settings, public_sound, catalog=None, localpacks=None):
        self.library, self.settings, self.public_sound = library, settings, public_sound
        self.catalog = catalog
        self.localpacks = localpacks
        self.cache = library.directory / 'quality-cache'
        self.cache.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.processing = threading.RLock()
        self.tasks: dict[str, dict] = {}
        self.events: dict[str, threading.Event] = {}
        self.adjusted: dict[str, dict] = {}
        with library.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS audio_checks (
                    sound_id TEXT PRIMARY KEY, data TEXT NOT NULL,
                    file_stamp TEXT NOT NULL, cache_name TEXT,
                    sound TEXT NOT NULL, checked_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sound_visibility (
                    sound_id TEXT PRIMARY KEY, hidden INTEGER NOT NULL
                );
            ''')
        self.register(app)

    def record(self, sound_id):
        with self.library.connection() as db:
            row = db.execute('SELECT * FROM audio_checks WHERE sound_id=?', (sound_id,)).fetchone()
        return dict(row) if row else None

    def cache_path(self, record):
        name = (record or {}).get('cache_name') or ''
        if not re.fullmatch(r'[0-9a-f]{32}\.(mp3|ogg|wav)', name):
            return None
        path = (self.cache / name).resolve()
        return path if path.parent == self.cache.resolve() and path.is_file() else None

    def file_stamp(self, sound, path):
        if sound.get('source') in {'catalog', 'localpack'} and not self.library.file(sound['id']):
            # These caches verify content before returning a path, and update
            # mtime for LRU access. Their immutable hashed filenames identify
            # content; a playback touch must not invalidate its own check.
            return f"verified-cache:{path.name}:{path.stat().st_size}"
        return stamp(path)

    def decorate(self, sound):
        result = sound.copy()
        sound_id = result['id']
        with self.library.connection() as db:
            pref = db.execute('SELECT hidden FROM sound_visibility WHERE sound_id=?', (sound_id,)).fetchone()
        result['hidden'] = bool(pref and pref['hidden'])
        path = self.library.file(sound_id)
        current_stamp = optional_stamp(path)
        if result.get('audio_check') and (not current_stamp or result.get('audio_check_stamp') != current_stamp
                                          or result['audio_check'].get('version') != CHECK_VERSION):
            result.pop('audio_check', None)
        record = self.record(sound_id)
        if record:
            candidate = path or self.cache_path(record)
            try:
                if candidate is None and result.get('source') == 'catalog' and self.catalog:
                    candidate = self.catalog.cached_file(sound_id)
                if candidate is None and result.get('source') == 'localpack' and self.localpacks:
                    candidate = self.localpacks.cached_file(sound_id)
            except (ValueError, providers.ProviderError, OSError):
                # A moved ZIP or retired catalog entry invalidates its cached
                # report, but must not prevent the rest of the library opening.
                candidate = None
            old_sound = json.loads(record['sound'])
            same_remote = old_sound.get('remote_preview_url') == result.get('remote_preview_url')
            try:
                candidate_stamp = self.file_stamp(result, candidate) if candidate else None
            except OSError:
                candidate_stamp = None
            valid = bool(candidate_stamp and candidate_stamp == record['file_stamp'])
            if not path:
                valid = valid and same_remote and time.time() - record['checked_at'] < 7 * 86400
            if valid:
                data = json.loads(record['data'])
                if data.get('version') == CHECK_VERSION:
                    result['audio_check'] = data
                    result.pop('audio_check_error', None)
                if data.get('error'):
                    result['audio_check_error'] = data['error']
        result.pop('audio_check_stamp', None)
        return result

    def resolve_sound(self, sound_id):
        sound = self.library.get(sound_id)
        if sound:
            return sound
        if sound_id.startswith('catalog-') and self.catalog:
            return self.catalog.detail(sound_id)
        if sound_id.startswith('localpack-') and self.localpacks:
            return self.localpacks.detail(sound_id)
        if re.fullmatch(r'ov-[0-9a-f-]{36}', sound_id):
            identifier = str(uuid.UUID(sound_id[3:]))
            if sound_id != 'ov-' + identifier:
                raise ValueError('在线音效编号无效。')
            return openverse.openverse_detail(identifier)
        if re.fullmatch(r'fs-\d{1,20}', sound_id):
            return providers.freesound_detail(sound_id[3:], self.settings.get())
        raise ValueError('音效不存在，请刷新列表后重试。')

    def prune(self, exclude=None):
        files = [p for p in self.cache.iterdir() if p.is_file() and
                 re.fullmatch(r'[0-9a-f]{32}\.(mp3|ogg|wav)', p.name)]
        total = sum(p.stat().st_size for p in files)
        # Only this module's disposable cache is ever reclaimed; original media is untouched.
        for path in sorted(files, key=lambda p: p.stat().st_mtime):
            if exclude and path == exclude:
                continue
            if total <= CACHE_BUDGET and time.time() - path.stat().st_mtime < 7 * 86400:
                continue
            size = path.stat().st_size
            try:
                path.unlink()
                total -= size
            except OSError:
                pass  # A currently playing Windows file can be retried next time.

    def file_for(self, sound, *, budget=None, cancelled=None):
        local = self.library.file(sound['id'])
        if local:
            return local, None
        record = self.record(sound['id'])
        cached = self.cache_path(record)
        if cached and time.time() - record['checked_at'] < 7 * 86400:
            previous = json.loads(record['sound'])
            if previous.get('remote_preview_url') == sound.get('remote_preview_url'):
                return cached, cached.name
        if cancelled and cancelled():
            raise ValueError('已取消音频检查。')
        if sound['source'] == 'catalog' and self.catalog:
            cached = self.catalog.cached_file(sound['id'])
            if cached:
                return cached, None
            raise ValueError('请先试听这条精选素材，再进行音频检查；检查不会自动下载整包。')
        if sound['source'] == 'localpack' and self.localpacks:
            cached = self.localpacks.cached_file(sound['id'])
            if cached:
                return cached, None
            raise ValueError('请先试听这条本地包素材，再进行音频检查。')
        if sound['source'] not in {'openverse', 'freesound'}:
            raise ValueError('本地音频已丢失，请重新导入。')
        remaining = BATCH_BUDGET - budget['bytes'] if budget is not None else FILE_BUDGET
        if remaining <= 0:
            raise ValueError('已达到本次 30MiB 下载预算，剩余音效尚未检测。')

        def count(size):
            if budget is not None:
                budget['bytes'] += size

        options = {'max_bytes': min(FILE_BUDGET, remaining), 'on_bytes': count, 'cancelled': cancelled}
        if sound['source'] == 'openverse':
            data, ext = openverse.download_openverse(sound, **options)
        else:
            data, ext, _ = providers.download_freesound(sound, self.settings.get(), 'preview', **options)
        if cancelled and cancelled():
            raise ValueError('已取消音频检查。')
        ext = ext.lstrip('.').lower()
        if ext not in {'mp3', 'ogg', 'wav'} or len(data) > min(FILE_BUDGET, remaining):
            raise ValueError('音频超出本次检查预算或格式不受支持。')
        self.prune()
        target = self.cache / (uuid.uuid4().hex + '.' + ext)
        target.write_bytes(data)
        return target, target.name

    def write_record(self, sound, data, path, cache_name):
        with self.library.connection() as db:
            db.execute('INSERT OR REPLACE INTO audio_checks VALUES (?,?,?,?,?,?)',
                       (sound['id'], json.dumps(data, ensure_ascii=False), self.file_stamp(sound, path), cache_name,
                        json.dumps(sound, ensure_ascii=False), time.time()))

    def inspect(self, sound_id, *, budget=None, cancelled=None):
        with self.processing:
            if cancelled and cancelled():
                raise ValueError('已取消音频检查。')
            sound = self.resolve_sound(sound_id)
            path, cache_name = self.file_for(sound, budget=budget, cancelled=cancelled)
            if cancelled and cancelled():
                raise ValueError('已取消音频检查。')
            record = self.record(sound_id)
            if record and record['file_stamp'] == self.file_stamp(sound, path):
                data = json.loads(record['data'])
                if data.get('version') == CHECK_VERSION:
                    return self.public_sound(sound), path, data
            try:
                data = quality.analyze_audio(path)
            except ValueError as error:
                self.write_record(sound, {'error': str(error)}, path, cache_name)
                raise
            self.write_record(sound, data, path, cache_name)
            return self.public_sound(sound), path, data

    def snapshot(self, task_id):
        with self.lock:
            if task_id not in self.tasks:
                raise HTTPException(404, '任务记录已过期或服务已重启，请刷新素材库。')
            result = deepcopy(self.tasks[task_id])
            result.pop('_key', None)
            return result

    def start(self, kind, key, total, work):
        with self.lock:
            for identifier, task in self.tasks.items():
                if task.get('_key') == key and task['status'] in {'queued', 'running'}:
                    return self.snapshot(identifier)
            if sum(t['status'] in {'queued', 'running'} for t in self.tasks.values()) >= 2:
                raise HTTPException(429, '已有两个音频任务，请等完成或取消后再试。')
            for identifier in list(self.tasks):
                if len(self.tasks) < 24:
                    break
                if self.tasks[identifier]['status'] not in {'queued', 'running'}:
                    self.tasks.pop(identifier)
                    self.events.pop(identifier, None)
            identifier = uuid.uuid4().hex
            event = threading.Event()
            self.events[identifier] = event
            self.tasks[identifier] = {'id': identifier, 'kind': kind, 'status': 'queued',
                                     'completed': 0, 'total': total, 'message': '等待开始',
                                     'items': [], 'errors': [], '_key': key}

        def update(**changes):
            with self.lock:
                self.tasks[identifier].update(changes)

        def worker():
            update(status='running')
            try:
                work(event, update)
                update(status='cancelled' if event.is_set() else 'complete',
                       message='已取消，已完成的结果已保留' if event.is_set() else '已完成')
            except gamepacks.PackCancelled:
                update(status='cancelled', message='已取消，已导入的声音已保留')
            except Exception as error:
                message = str(error) if isinstance(error, (ValueError, providers.ProviderError)) else '本机处理未完成，请检查磁盘空间后重试。'
                update(status='cancelled' if event.is_set() else 'failed', message=message, errors=[message])

        threading.Thread(target=worker, name='soundbox-' + kind, daemon=True).start()
        return self.snapshot(identifier)

    def compensation(self, sound_id):
        with self.processing:
            sound, source, report = self.inspect(sound_id)
            if report['status'] != 'ready':
                raise ValueError('这条音频不可用，无法生成音量补偿。')
            key = hashlib.sha256((str(source) + self.file_stamp(sound, source) + str(CHECK_VERSION)).encode()).hexdigest()
            cached = self.adjusted.get(key)
            if cached and cached['path'].is_file():
                return cached
            self.prune(exclude=source)
            token = uuid.uuid4().hex
            target = self.cache / (token + '.wav')
            result = quality.render_compensated(source, target, report)
            waveform = inspect_audio(target)
            item = {'path': target, 'token': token, 'sound': sound, 'key': key,
                    'gain_db': result.get('applied_gain_db', report['gain_db']),
                    'duration': waveform['duration'], 'waveform': waveform['waveform'],
                    'message': '仅调节音量并保留瞬态，不做降噪或裁尾。', 'audio_check': quality.analyze_audio(target)}
            self.adjusted[key] = item
            if len(self.adjusted) > 128:
                self.adjusted.pop(next(iter(self.adjusted)))
            return item

    def register(self, app):
        @app.get('/api/game-packs')
        def packs():
            return {'items': gamepacks.list_packs(self.library)}

        @app.post('/api/game-packs/{pack_id}/install')
        def install(pack_id: str):
            pack = next((p for p in gamepacks.list_packs(self.library) if p['id'] == pack_id), None)
            if pack is None:
                raise ValueError('游戏素材包不存在，请刷新列表。')

            def work(event, update):
                items = gamepacks.install_pack(pack_id, self.library,
                    progress=lambda done, total, message: update(completed=done, total=total, message=message),
                    cancelled=event.is_set)
                update(items=[self.public_sound(s) for s in items])

            return self.start('pack', 'pack:' + pack_id, pack['count'], work)

        @app.post('/api/audio-checks')
        def check(body: CheckRequest):
            ids = list(dict.fromkeys(body.ids))
            if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', identifier) for identifier in ids):
                raise ValueError('音效编号无效，请刷新列表。')

            def work(event, update):
                items, errors, budget = [], [], {'bytes': 0}
                for index, sound_id in enumerate(ids):
                    if event.is_set():
                        break
                    update(message=f'正在检查 {index + 1}/{len(ids)}')
                    try:
                        sound, _, _ = self.inspect(sound_id, budget=budget, cancelled=event.is_set)
                        items.append(sound)
                    except (ValueError, providers.ProviderError) as error:
                        if event.is_set():
                            break
                        errors.append(str(error))
                    update(completed=index + 1, items=list(items), errors=list(errors), downloaded_bytes=budget['bytes'])

            return self.start('check', 'check:' + '|'.join(ids), len(ids), work)

        @app.get('/api/audio-tasks/{task_id}')
        def task(task_id: str):
            snapshot = self.snapshot(task_id)
            snapshot.pop('_key', None)
            return snapshot

        @app.post('/api/audio-tasks/{task_id}/cancel')
        def cancel(task_id: str):
            self.snapshot(task_id)
            with self.lock:
                self.events[task_id].set()
            return task(task_id)

        @app.patch('/api/sounds/{sound_id}/visibility')
        def visibility(sound_id: str, body: Visibility):
            if not self.library.get(sound_id) and not (re.fullmatch(r'fs-\d{1,20}', sound_id) or
                    re.fullmatch(r'ov-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', sound_id)):
                raise ValueError('音效编号无效，请刷新列表。')
            with self.library.connection() as db:
                db.execute('INSERT OR REPLACE INTO sound_visibility VALUES (?,?)', (sound_id, int(body.hidden)))
            return {'id': sound_id, 'hidden': body.hidden}

        @app.post('/api/sounds/{sound_id}/compensate')
        def compensate(sound_id: str):
            result = self.compensation(sound_id)
            return {k: result[k] for k in ('gain_db', 'duration', 'waveform', 'message')} | {
                'preview_url': '/api/audio-adjusted/' + result['token']}

        @app.get('/api/audio-adjusted/{token}')
        def adjusted_audio(token: str):
            if not re.fullmatch(r'[0-9a-f]{32}', token):
                raise HTTPException(404, '试听不存在。')
            path = self.cache / (token + '.wav')
            if not path.is_file():
                raise HTTPException(404, '试听缓存已过期，请重新生成音量补偿。')
            return FileResponse(path, media_type='audio/wav')

        @app.post('/api/sounds/{sound_id}/compensate/save')
        def save(sound_id: str, body: SaveCompensation):
            with self.processing:
                self.library.check_folder(body.folder_id)
                if body.name is not None and not body.name.strip():
                    raise ValueError('名称不能为空。')
                result = self.compensation(sound_id)
                # Repeated requests for the same source/version return the already saved copy.
                identifier = 'level-' + result['key'][:32]
                previous = self.library.get(identifier)
                if previous and self.library.file(identifier):
                    return self.public_sound(previous)
                original = result['sound']
                target = self.library.media / (identifier + '-' + uuid.uuid4().hex[:8] + '.wav')
                sound = base_sound(identifier, body.name.strip() if body.name else original['name'][:180] + ' · 音量补偿', 'edited')
                sound.update(provenance([original]))
                sound.update({'quality': 'edited', 'tags': list(original.get('tags', [])),
                              'folder_id': body.folder_id, 'duration': result['duration'], 'waveform': result['waveform'],
                              'description': f"音量补偿 {result['gain_db']:+.2f}dB；保留原素材，未降噪或裁尾。",
                              'audio_check': result['audio_check'],
                              'compensation': {'source_id': sound_id, 'gain_db': result['gain_db']}})
                try:
                    shutil.copyfile(result['path'], target)
                    sound['audio_check_stamp'] = stamp(target)
                    self.library.put(sound, target)
                except Exception:
                    # A commit may have succeeded before an I/O response failed.
                    # Never remove a file already referenced by the saved library row.
                    try:
                        committed = self.library.file(identifier) == target
                    except Exception:
                        committed = True  # Preserve recovery data when the DB itself is unavailable.
                    if not committed:
                        target.unlink(missing_ok=True)
                    raise ValueError('补偿版保存未完成，请检查磁盘空间后重试；原素材保持不变。') from None
                return self.public_sound(sound)
