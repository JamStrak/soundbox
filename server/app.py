"""Local HTTP API for the Soundbox game sound-effects workbench."""
from __future__ import annotations

import csv
import hashlib
from contextlib import asynccontextmanager
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import threading
import uuid
from urllib.parse import urlparse
import zipfile

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import openverse, providers, video_api
from .catalog import Catalog
from .localpacks import LocalPacks, LocalPackCancelled
from .audio import (ALLOWED_EXTENSIONS, MAX_BYTES, base_sound, inspect_audio, make_browser_preview,
                    seed_library, browser_audio_path, browser_audio_mime)
from .search import local_match
from .settings import Settings
from .storage import Library, now
from .editor_api import register_editor
from .quality_api import AudioService, filtered
from .hub_lifecycle import HubLifecycle, HubMiddleware

ROOT = Path(__file__).resolve().parent.parent
VERSION = '0.1.0'


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class FolderCreate(Model):
    name: str = Field(min_length=1, max_length=80)
    parent_id: str | None = None

    @field_validator('name')
    @classmethod
    def trim_name(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError('名称不能为空或包含控制字符')
        return value


class FolderUpdate(Model):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    parent_id: str | None = None


class SoundUpdate(Model):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    tags: list[str] | None = Field(default=None, max_length=40)
    note: str | None = Field(default=None, max_length=5000)
    favorite: bool | None = None
    folder_id: str | None = None


class SaveSound(Model):
    sound: dict
    download: bool = False
    favorite: bool | None = None
    folder_id: str | None = None


class LocalPackIndex(Model):
    path: str = Field(min_length=1, max_length=1500)

    @field_validator('path')
    @classmethod
    def clean_path(cls, value):
        value = value.strip().strip('"')
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError('请输入本机音效包文件夹或 ZIP 路径。')
        return value


class Download(Model):
    quality: str = Field(default='preview', pattern='^(preview|original)$')


class Translation(Model):
    text: str = Field(min_length=1, max_length=2000)
    use_ai: bool = False


class TagSuggestionRequest(Model):
    name: str = Field(min_length=1, max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=40)
    description: str = Field(default='', max_length=2000)

    @field_validator('name')
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError('音效名称不能为空或包含控制字符')
        return value

    @field_validator('tags')
    @classmethod
    def clean_tags(cls, values):
        cleaned = []
        for value in values:
            value = value.strip()
            if len(value) > 120 or any(ord(char) < 32 for char in value):
                raise ValueError('标签过长或包含控制字符')
            if value:
                cleaned.append(value)
        return cleaned

    @field_validator('description')
    @classmethod
    def clean_description(cls, value):
        value = value.replace('\r', ' ').strip()
        if any(ord(char) < 32 and char not in '\n\t' for char in value):
            raise ValueError('描述包含控制字符')
        return value


class Export(Model):
    ids: list[str] = Field(min_length=1, max_length=100)


class Generate(Model):
    prompt: str = Field(min_length=1, max_length=2000)
    duration: float = Field(ge=.5, le=30)
    loop: bool = False
    folder_id: str | None = None


class ConnectionTest(Model):
    provider: str = Field(pattern='^(freesound|elevenlabs|text)$')


class Authorize(Model):
    code: str = Field(min_length=1, max_length=1000)


class SettingsUpdate(Model):
    freesound_key: str | None = Field(default=None, max_length=1000)
    freesound_client_id: str | None = Field(default=None, max_length=300)
    elevenlabs_key: str | None = Field(default=None, max_length=1000)
    elevenlabs_license: str | None = Field(default=None, pattern='^(unknown|personal|commercial)$')
    text_api_key: str | None = Field(default=None, max_length=1000)
    text_api_base: str | None = Field(default=None, max_length=1000)
    text_model: str | None = Field(default=None, max_length=300)
    ai_translation: bool | None = None


class TextModels(Model):
    text_api_base: str | None = Field(default=None, max_length=1000)
    text_api_key: str | None = Field(default=None, max_length=1000)


def safe_filename(value: str) -> str:
    if Path(value).suffix.lower() in ALLOWED_EXTENSIONS | {'.aif', '.aiff'}:
        value = value[:-len(Path(value).suffix)]
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip(' .')[:140] or 'sound'
    if value.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))}:
        value = '_' + value
    return value


def license_allowed(sound: dict, requested: str) -> bool:
    if requested == 'all':
        return True
    value = (str(sound.get('license', '')) + ' ' + str(sound.get('license_url', ''))).lower()
    cc0 = 'cc0' in value or '/publicdomain/zero/' in value
    if requested == 'cc0':
        return cc0
    if any(term in value for term in ('noncommercial', 'by-nc', 'by-nd', 'by-sa-nd', 'personal', 'no derivatives')):
        return False
    return (cc0 or '/publicdomain/mark/' in value or '/licenses/by/' in value or '/licenses/by-sa/' in value
            or bool(re.fullmatch(r'cc[ -]by(?:-sa)?(?: \d+(?:\.\d+)?)?', str(sound.get('license', '')).lower()))
            or sound.get('license') == 'Commercial'
            or (sound.get('source') == 'localpack' and sound.get('license') == 'Sonniss GDC 项目使用许可'
                and sound.get('license_url') == 'https://sonniss.com/gdc-bundle-license/'))


def create_app(data_dir: str | Path | None = None) -> FastAPI:
    directory = Path(data_dir or os.getenv('SOUNDBOX_DATA_DIR', str(ROOT / 'data'))).resolve()
    library = Library(directory)
    settings = Settings(directory)
    catalog = Catalog(directory)
    localpacks = LocalPacks(directory)
    jobs: dict[str, dict] = {}
    jobs_lock = threading.RLock()
    downloads_lock = threading.RLock()
    translation_lock = threading.RLock()
    translation_cache: dict[tuple, dict] = {}
    preview_lock = threading.RLock()
    instance = uuid.uuid4().hex

    @asynccontextmanager
    async def lifespan(application):
        if os.getenv('SOUNDBOX_NO_SEED') != '1':
            seed_library(library)
        yield

    app = FastAPI(title='声匣 · 游戏音效工作台', version=VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.state.library = library
    app.state.settings = settings
    app.state.catalog = catalog
    app.state.localpacks = localpacks

    @app.middleware('http')
    async def local_only(request: Request, call_next):
        host = request.url.hostname
        if host not in {'127.0.0.1', 'localhost', '::1', 'testserver'}:
            return JSONResponse({'detail': '仅允许从本机打开声匣。'}, status_code=403)
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            if request.headers.get('X-Soundbox-Client') != 'local':
                return JSONResponse({'detail': '请求来源无效，请从声匣页面重试。'}, status_code=403)
            origin = request.headers.get('origin')
            if origin and origin != str(request.base_url).rstrip('/'):
                return JSONResponse({'detail': '不允许其他网站修改本机素材库。'}, status_code=403)
            try:
                size = int(request.headers.get('content-length', '0'))
            except ValueError:
                return JSONResponse({'detail': '请求长度无效。'}, status_code=400)
            video_upload = request.method == 'POST' and video_api.video_upload_path(request.url.path)
            limit = video_api.MAX_VIDEO_BYTES + video_api.MULTIPART_OVERHEAD if video_upload else 105 * 1024 * 1024
            if size < 0:
                return JSONResponse({'detail': '请求长度无效。'}, status_code=400)
            if size > limit:
                message = ('视频不能超过 512 MB，请先压缩或截短后重试。' if video_upload
                           else '一次导入总大小不能超过 100MB，请分批导入。')
                return JSONResponse({'detail': message}, status_code=413)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        if request.url.path.startswith('/api/') and not request.url.path.startswith('/api/audio/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(providers.ProviderError)
    async def provider_error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=getattr(exc, 'status_code', 400))

    @app.exception_handler(ValueError)
    async def input_error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Pydantic's default error includes the original input, potentially a
        # mistyped API key. Never return raw request values in validation errors.
        return JSONResponse({'detail': '请求参数无效，请检查输入长度、格式和必填项。'}, status_code=422)

    def search_query(text: str, configuration: dict, use_ai: bool) -> dict:
        if not use_ai or not text.strip():
            return providers.translate_query(text, configuration, False)
        fingerprint = hashlib.sha256(json.dumps({k: configuration[k] for k in (
            'text_api_key', 'text_api_base', 'text_model')}, sort_keys=True).encode()).hexdigest()
        cache_key = (text.strip(), fingerprint)
        # Reuse the user's explicit query interpretation for library, online,
        # pagination and filter requests; never spend tokens on each refresh.
        with translation_lock:
            if cache_key not in translation_cache:
                if len(translation_cache) >= 128:
                    translation_cache.pop(next(iter(translation_cache)))
                translation_cache[cache_key] = providers.translate_query(text, configuration, True)
            return translation_cache[cache_key].copy()

    def get_sound(sound_id):
        sound = library.get(sound_id)
        if sound is None:
            raise HTTPException(404, '音效不存在或已移出素材库，请刷新。')
        return sound

    def public_sound(sound: dict) -> dict:
        result = sound.copy()
        local_file = library.file(sound['id'])
        result['downloaded'] = bool(local_file)
        if local_file:
            result['preview_url'] = f"/api/audio/{sound['id']}"
        return audio_service.decorate(result)

    def ensure_browser_preview(path: Path):
        # Repeated Range requests must not re-encode an existing compatibility file.
        with preview_lock:
            if browser_audio_path(path) == path:
                make_browser_preview(path)
            return browser_audio_path(path)

    audio_service = AudioService(app, library, settings, public_sound, catalog=catalog, localpacks=localpacks)
    app.state.audio_service = audio_service
    video_sources = video_api.register_video(app, library)
    register_editor(app, library, public_sound, video_sources=video_sources)

    def hub_jobs():
        with jobs_lock, audio_service.lock:
            states = [item.get('status') for item in jobs.values()]
            states.extend(item.get('status') for item in audio_service.tasks.values())
        return {'activeJobs': states.count('running'), 'queuedJobs': states.count('queued')}

    app.state.hub_lifecycle = HubLifecycle(
        'soundbox', ROOT, hub_jobs, instance_id=instance,
        health_paths={'/api/health'}, static_paths={'/', '/index.html', '/favicon.svg'},
        media_paths=[r'/api/audio/[^/]+', r'/api/audio-adjusted/[^/]+', r'/api/catalog/audio/[^/]+',
                     r'/api/local-packs/audio/[^/]+',
                     r'/api/video-sources/[0-9a-f]{32}/audio',
                     r'/api/editor/previews/[^/]+', r'/api/sounds/[^/]+/file'],
        model_note='声匣使用远程生成 API 与本机音频处理，没有常驻本地模型可单独释放。')
    # Added last, hence outside local_only: Hub endpoints have their own
    # same-origin/token validation, while all business responses stay counted.
    app.add_middleware(HubMiddleware, lifecycle=app.state.hub_lifecycle)

    def perform_download(sound_id: str, quality: str) -> dict:
        with library.lock:
            sound = get_sound(sound_id)
            if sound['source'] not in {'freesound', 'openverse', 'catalog', 'localpack'}:
                if library.file(sound_id):
                    return public_sound(sound)
                raise HTTPException(400, '本地音频已丢失，请重新导入。')
            if sound['source'] == 'openverse':
                if quality == 'original':
                    raise HTTPException(400, 'Openverse 提供公开音频；如需其他格式的原文件，请打开素材来源页面。')
            if library.file(sound_id) and (sound.get('quality') == 'original' or quality == 'preview'):
                return public_sound(sound)
        if sound['source'] == 'localpack':
            path = library.media / (sound_id + '-' + uuid.uuid4().hex[:8] + '.wav')
            try:
                with localpacks.lease_file(sound_id) as source_path:
                    shutil.copyfile(source_path, path)
                analysis = inspect_audio(path)
                make_browser_preview(path)
                with library.lock:
                    sound = get_sound(sound_id)
                    sound.update(analysis | {'downloaded': True, 'quality': 'original',
                                            'preview_url': f'/api/audio/{sound_id}', 'format': path.suffix[1:]})
                    return public_sound(library.put(sound, path))
            except Exception:
                path.unlink(missing_ok=True)
                path.with_name(path.stem + '.listen.mp3').unlink(missing_ok=True)
                raise
        configuration = settings.get()

        def persist_tokens(updates):
            if updates:
                with settings.lock:
                    current = settings.get()
                    if all(current[k] == configuration[k] for k in ('freesound_key', 'freesound_client_id', 'freesound_refresh_token')):
                        settings.update(updates)

        try:
            if sound['source'] == 'catalog':
                audio, ext = catalog.download(sound_id)
                quality = 'original'
                updates = {}
            elif sound['source'] == 'openverse':
                audio, ext = openverse.download_openverse(sound)
                updates = {}
            else:
                audio, ext, updates = providers.download_freesound(sound, configuration, quality)
        except providers.ProviderError as exc:
            persist_tokens(getattr(exc, 'updated_tokens', {}))
            raise
        persist_tokens(updates)
        ext = '.' + ext.lstrip('.').lower()
        if ext not in ALLOWED_EXTENSIONS | {'.aif', '.aiff'} or len(audio) > MAX_BYTES:
            raise ValueError('音频格式不受支持或超过 50MB。')
        # Stage separately so a corrupt replacement never destroys the cached original.
        path = library.media / (sound_id + '-' + uuid.uuid4().hex[:8] + ext)
        path.write_bytes(audio)
        try:
            analysis = inspect_audio(path)
            make_browser_preview(path)
            with library.lock:
                sound = get_sound(sound_id)
                sound.update(analysis | {'downloaded': True, 'quality': quality,
                                         'preview_url': f'/api/audio/{sound_id}', 'format': ext[1:]})
                return library.put(sound, path)
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def download_sound(sound_id: str, quality: str) -> dict:
        # Serializing local downloads avoids OAuth refresh-token rotation races
        # and a late preview overwriting an already downloaded original.
        with downloads_lock:
            return perform_download(sound_id, quality)

    @app.get('/api/health')
    def health():
        return {'app': 'soundbox', 'version': VERSION, 'instance': instance,
                'root': str(ROOT), 'data_dir': str(directory)}

    @app.get('/api/stats')
    def stats():
        sounds = library.all()
        return {'total': len(sounds), 'favorites': sum(bool(s.get('favorite')) for s in sounds),
                'generated': sum(s['source'] == 'elevenlabs' for s in sounds), 'folders': len(library.folders())}

    @app.get('/api/library')
    def list_library(q: str = Query(default='', max_length=2000), favorite: bool = False,
                     folder_id: str = '', source: str = '', duration_max: float = Query(default=0, ge=0, le=600),
                     license: str = Query(default='all', pattern='^(all|commercial|cc0)$'),
                     sort: str = Query(default='newest', pattern='^(newest|name|duration)$'),
                     visibility: str = Query(default='visible', pattern='^(visible|all|hidden)$'),
                     audio_filter: str = Query(default='all', pattern='^(all|checked|quiet|issues|unchecked)$'),
                     pack_only: bool = False, collapse_variants: bool = False, use_ai: bool = False):
        sounds = library.all()
        query = search_query(q, settings.get(), use_ai) if q.strip() else None
        if pack_only:
            sounds = [s for s in sounds if s.get('source') in {'kenney', 'catalog', 'localpack'}]
        if favorite:
            sounds = [s for s in sounds if s.get('favorite')]
        if folder_id and folder_id != 'all':
            sounds = [s for s in sounds if s.get('folder_id') == (None if folder_id == 'unfiled' else folder_id)]
        if source and source != 'all':
            sounds = [s for s in sounds if s['source'] == source]
        if duration_max:
            sounds = [s for s in sounds if s.get('duration', 0) <= duration_max]
        sounds = [s for s in sounds if license_allowed(s, license)]
        if q.strip():
            search_text = query['translated'] if query else q
            sounds = [s for s in sounds if local_match(search_text, ' '.join([
                s['name'], ' '.join(s.get('tags', [])), s.get('description', ''), s.get('note', '')]))]
        sounds.sort(key=lambda s: s.get({'newest': 'created_at', 'name': 'name', 'duration': 'duration'}[sort], ''),
                    reverse=sort == 'newest')
        sounds, hidden_count = filtered([public_sound(s) for s in sounds], visibility, audio_filter)
        total = len(sounds)
        if collapse_variants:
            groups = {}
            for sound in sounds:
                key = sound.get('variant_group') or sound['id']
                if key not in groups:
                    groups[key] = sound | {'variant_count': 1}
                else:
                    groups[key]['variant_count'] += 1
            sounds = list(groups.values())
        result = {'items': sounds, 'total': total, 'hidden_count': hidden_count, 'collapsed_count': total - len(sounds)}
        if use_ai and query:
            result['query'] = query
            if query.get('warning'):
                result['warning'] = query['warning']
        return result

    @app.get('/api/search')
    def online_search(q: str = Query(default='', max_length=2000), page: int = Query(default=1, ge=1, le=1000),
                      license: str = Query(default='all', pattern='^(all|commercial|cc0)$'),
                      duration_max: float = Query(default=0, ge=0, le=600), sort: str = 'score',
                      provider: str = Query(default='openverse', pattern='^(openverse|freesound|catalog)$'),
                      pack_id: str = Query(default='', max_length=100),
                      use_ai: bool = False,
                      visibility: str = Query(default='visible', pattern='^(visible|all|hidden)$'),
                      audio_filter: str = Query(default='all', pattern='^(all|checked|quiet|issues|unchecked)$')):
        configuration = settings.get()
        if provider == 'freesound' and not configuration['freesound_key']:
            raise HTTPException(400, 'Freesound 直连需要 API Key。可以切换到 Openverse 免密钥搜索，或在设置中填写 Key。')
        query = search_query(q, configuration, use_ai)
        if provider == 'catalog':
            result = catalog.search(query, page, license, duration_max, sort, pack_id=pack_id)
        elif provider == 'openverse':
            result = openverse.search_openverse(query, page, license, duration_max, sort)
        else:
            result = providers.search_freesound(query, configuration, page, license, duration_max, sort)
        result['items'] = [public_sound(library.get(s['id']) or s) for s in result['items']]
        result['items'], result['hidden_count'] = filtered(result['items'], visibility, audio_filter)
        if audio_filter != 'all' or visibility != 'all' and result['hidden_count']:
            result['warning'] = (result.get('warning', '') + ' 音频检查与隐藏筛选仅作用于本页，未检测不代表质量差；上游总数不变。').strip()
        return result

    @app.get('/api/catalog/packs')
    def catalog_packs():
        # Reading the pinned catalog never downloads a pack or calls a paid API.
        return {'items': catalog.packs()}

    @app.post('/api/catalog/previews/{sound_id}')
    def catalog_preview(sound_id: str):
        sound = catalog.detail(sound_id)
        local = library.file(sound_id)
        if local:
            return public_sound(get_sound(sound_id))
        with preview_lock:
            path = catalog.audio_file(sound_id)
            analysis = inspect_audio(path)
            ensure_browser_preview(path)
            with library.lock:
                existing = library.get(sound_id)
                if existing:
                    sound = library.put(existing | analysis)
            return public_sound(sound | analysis | {'catalog_cached': True})

    @app.get('/api/catalog/audio/{sound_id}')
    def catalog_audio(sound_id: str):
        # IDs resolve exclusively through the server-owned, hashed manifest.
        path = catalog.audio_file(sound_id)
        playable = ensure_browser_preview(path)
        return FileResponse(playable, media_type=browser_audio_mime(playable))

    @app.get('/api/local-packs')
    def local_pack_status():
        sounds = localpacks.all()
        packs = {}
        for sound in sounds:
            pack_id = sound['pack_id']
            if pack_id not in packs:
                packs[pack_id] = {'id': pack_id, 'name': sound.get('pack_name', ''),
                                  'count': 0, 'license': sound.get('license', '')}
            packs[pack_id]['count'] += 1
        return {'items': list(packs.values()), 'total': len(sounds)}

    @app.post('/api/local-packs/index')
    def index_local_packs(body: LocalPackIndex):
        def work(event, update):
            try:
                result = localpacks.index_directory(body.path,
                    progress=lambda done, total, message: update(completed=done, total=total, message=message),
                    cancelled=event.is_set)
            except LocalPackCancelled as error:
                result = error.summary
            saved = []
            for item in result['items']:
                with library.lock:
                    existing = library.get(item['id'])
                    if existing:
                        # Reindexing a user's pack must keep personal edits and downloaded originals.
                        if existing.get('source') != 'localpack':
                            raise ValueError('音效编号冲突，未覆盖现有素材。')
                        item = item | {key: existing.get(key) for key in
                                       ('name', 'tags', 'note', 'favorite', 'folder_id', 'created_at')}
                        item['saved'] = True
                        if library.file(item['id']):
                            item = existing
                    else:
                        item = item | {'saved': True, 'created_at': now()}
                    saved.append(public_sound(library.put(item)))
            update(items=saved, errors=result.get('skipped', []))
        return audio_service.start('local-pack', 'index:' + hashlib.sha256(body.path.encode()).hexdigest(), 0, work)

    @app.post('/api/local-packs/previews/{sound_id}')
    def local_pack_preview(sound_id: str):
        sound = localpacks.detail(sound_id)
        with preview_lock:
            path = library.file(sound_id)
            if path:
                analysis = inspect_audio(path)
                ensure_browser_preview(path)
            else:
                with localpacks.lease_file(sound_id) as path:
                    analysis = inspect_audio(path)
                    ensure_browser_preview(path)
            with library.lock:
                existing = library.get(sound_id)
                if existing:
                    sound = library.put(existing | analysis)
            return public_sound(sound | analysis | {'catalog_cached': True})

    @app.get('/api/local-packs/audio/{sound_id}')
    def local_pack_audio(sound_id: str):
        localpacks.detail(sound_id)
        path = library.file(sound_id)
        if path:
            playable = ensure_browser_preview(path)
        else:
            with localpacks.lease_file(sound_id) as path:
                playable = ensure_browser_preview(path)
        return FileResponse(playable, media_type=browser_audio_mime(playable))

    @app.post('/api/translate')
    def translate(body: Translation):
        return providers.translate_query(body.text, settings.get(), body.use_ai)

    @app.post('/api/tag-suggestions')
    def tag_suggestions(body: TagSuggestionRequest):
        return {'tags': providers.suggest_sound_tags(body.name, body.tags, body.description, settings.get())}

    @app.get('/api/folders')
    def folders():
        return {'items': library.folders()}

    @app.post('/api/folders')
    def create_folder(body: FolderCreate):
        return library.add_folder(body.name, body.parent_id)

    @app.patch('/api/folders/{folder_id}')
    def change_folder(folder_id: str, body: FolderUpdate):
        changes = body.model_dump(exclude_unset=True)
        if 'name' in changes:
            if changes['name'] is None or not changes['name'].strip():
                raise ValueError('文件夹名称不能为空。')
            changes['name'] = FolderCreate(name=changes['name']).name
        return library.update_folder(folder_id, changes)

    @app.delete('/api/folders/{folder_id}')
    def delete_folder(folder_id: str):
        library.remove_folder(folder_id)
        return {'ok': True}

    @app.post('/api/sounds/save')
    def save_sound(body: SaveSound):
        sound_id = str(body.sound.get('id', ''))
        existing = library.get(sound_id)
        if existing:
            sound = existing
        else:
            source = body.sound.get('source')
            if source == 'localpack':
                sound = localpacks.detail(sound_id)
            elif source == 'catalog':
                sound = catalog.detail(sound_id)
            elif source == 'openverse':
                source_id = str(body.sound.get('source_id') or sound_id.removeprefix('ov-'))
                try:
                    canonical_id = str(uuid.UUID(source_id))
                except ValueError:
                    raise ValueError('在线音效编号无效，请重新搜索。') from None
                if sound_id != 'ov-' + canonical_id:
                    raise ValueError('在线音效编号不匹配，请重新搜索。')
                sound = openverse.openverse_detail(canonical_id)
            elif source == 'freesound':
                source_id = str(body.sound.get('source_id') or sound_id.removeprefix('fs-'))
                if not re.fullmatch(r'\d+', source_id) or sound_id != 'fs-' + source_id:
                    raise ValueError('在线音效编号无效，请重新搜索。')
                sound = providers.freesound_detail(source_id, settings.get())
            else:
                raise ValueError('请从实际在线检索结果保存音效，或使用本地导入。')
            sound.update({'saved': True, 'downloaded': False, 'favorite': False, 'folder_id': None,
                          'note': '', 'created_at': now(), 'quality': 'original' if source in {'catalog', 'localpack'} else 'preview'})
            sound.setdefault('waveform', [])
        changes = body.model_dump(exclude_unset=True)
        if 'favorite' in changes and body.favorite is not None:
            sound['favorite'] = body.favorite
        if 'folder_id' in changes:
            library.check_folder(body.folder_id)
            sound['folder_id'] = body.folder_id
        library.put(sound)
        return download_sound(sound['id'], 'preview') if body.download else public_sound(sound)

    @app.patch('/api/sounds/{sound_id}')
    def edit_sound(sound_id: str, body: SoundUpdate):
        changes = body.model_dump(exclude_unset=True)
        if 'folder_id' in changes:
            library.check_folder(body.folder_id)
        if 'name' in changes:
            if body.name is None or not body.name.strip():
                raise ValueError('音效名称不能为空。')
            changes['name'] = body.name.strip()
        if 'tags' in changes:
            if body.tags is None or any(len(t) > 80 for t in body.tags):
                raise ValueError('标签不能为空值，每个标签最多 80 个字符。')
            changes['tags'] = list(dict.fromkeys(t.strip() for t in body.tags if t.strip()))
        for key in ('favorite', 'note'):
            if key in changes and changes[key] is None:
                raise ValueError('收藏或备注值无效。')
        return public_sound(library.update(sound_id, changes))

    @app.delete('/api/sounds/{sound_id}')
    def remove_sound(sound_id: str):
        get_sound(sound_id)
        library.remove(sound_id)
        return {'ok': True}

    @app.post('/api/sounds/{sound_id}/download')
    def download(sound_id: str, body: Download):
        return download_sound(sound_id, body.quality)

    @app.get('/api/audio/{sound_id}')
    def audio_stream(sound_id: str):
        path = library.file(sound_id)
        if path is None:
            raise HTTPException(404, '音频尚未下载或文件已丢失。请先下载音效。')
        playable = browser_audio_path(path)
        return FileResponse(playable, media_type=browser_audio_mime(playable))

    @app.get('/api/sounds/{sound_id}/file')
    def download_file(sound_id: str):
        sound = get_sound(sound_id)
        path = library.file(sound_id)
        if path is None:
            raise HTTPException(400, '这条音效还没有本地文件，请先下载预览或原文件。')
        return FileResponse(path, filename=safe_filename(sound['name']) + path.suffix)

    @app.post('/api/import')
    def import_audio(files: list[UploadFile] = File(...), folder_id: str | None = Form(default=None)):
        if len(files) > 20:
            raise ValueError('一次最多导入 20 条音效，请分批添加。')
        folder_id = folder_id or None
        library.check_folder(folder_id)
        items, errors = [], []
        for upload in files:
            original_name = Path((upload.filename or 'sound').replace('\\', '/')).name
            extension = Path(original_name).suffix.lower()
            if extension not in ALLOWED_EXTENSIONS:
                errors.append(f'{original_name}：不支持此格式，请使用 WAV、MP3、OGG、FLAC、M4A 或 AAC。')
                continue
            sound_id = uuid.uuid4().hex
            path = library.media / (sound_id + extension)
            try:
                total = 0
                with path.open('wb') as output:
                    while chunk := upload.file.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_BYTES:
                            raise ValueError('文件超过 50MB，请先裁剪或压缩。')
                        output.write(chunk)
                analysis = inspect_audio(path)
                make_browser_preview(path)
                sound = base_sound(sound_id, Path(original_name).stem[:200], 'local', extension) | analysis
                sound.update({'folder_id': folder_id, 'description': '本地导入素材；使用前请确认原素材授权。'})
                items.append(library.put(sound, path))
            except (ValueError, OSError) as exc:
                path.unlink(missing_ok=True)
                errors.append(f'{original_name}：{exc}')
            finally:
                upload.file.close()
        return {'items': items, 'errors': errors}

    @app.post('/api/export')
    def export_audio(body: Export):
        rows = []
        for sound_id in dict.fromkeys(body.ids):
            sound = get_sound(sound_id)
            path = library.file(sound_id)
            if path is None:
                raise ValueError(f'“{sound["name"]}”尚未下载。请先保存音频后再批量导出。')
            rows.append((sound, path))
        total_size = sum(path.stat().st_size for _, path in rows)
        if total_size > 300 * 1024 * 1024:
            raise ValueError('这批音效超过 300MB，请分成更小的批次导出。')
        buffer = io.BytesIO()
        attribution = io.StringIO(newline='')
        writer = csv.writer(attribution)
        writer.writerow(['文件', '名称', '作者', '许可', '许可链接', '来源', '来源链接', '文件质量', '备注', '原始来源', '署名说明'])

        def csv_safe(value):
            value = str(value)
            return "'" + value if value.startswith(('=', '+', '-', '@', '\t', '\r')) else value

        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            for index, (sound, path) in enumerate(rows, 1):
                name = f'{index:03d}_{safe_filename(sound["name"])}{path.suffix}'
                archive.write(path, name)
                writer.writerow([csv_safe(v) for v in [name, sound['name'], sound.get('author', ''),
                    sound.get('license', ''), sound.get('license_url', ''), sound['source'],
                    sound.get('source_url', ''), sound.get('quality', ''), sound.get('note', ''),
                    sound.get('origin_source', sound['source']), sound.get('attribution', '')]])
            archive.writestr('attribution.csv', '\ufeff' + attribution.getvalue())
        return Response(buffer.getvalue(), media_type='application/zip',
                        headers={'Content-Disposition': 'attachment; filename="soundbox-export.zip"'})

    @app.get('/api/settings')
    def get_settings():
        return settings.public()

    @app.patch('/api/settings')
    def update_settings(body: SettingsUpdate):
        changes = {k: v.strip() if isinstance(v, str) else v
                   for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
        base = changes.get('text_api_base')
        if base:
            providers.validate_text_api_base(base)
            parsed = urlparse(base)
            if parsed.username or parsed.password or not parsed.hostname or parsed.query or parsed.fragment:
                raise ValueError('文本 AI 地址必须是无账号、无查询参数的接口基础地址。')
            if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1', '::1'}):
                raise ValueError('文本 AI 使用 HTTPS 地址；本机模型可以使用 http://127.0.0.1。')
        previous = settings.get()
        if ('text_api_base' in changes and changes['text_api_base'].rstrip('/') != previous['text_api_base'].rstrip('/')
                and previous['text_api_key'] and 'text_api_key' not in changes):
            raise ValueError('切换文本 AI 地址时，请重新填写该服务的 API Key，或先清除旧密钥。')
        if any(k in changes and changes[k] != previous[k] for k in ('freesound_key', 'freesound_client_id')):
            changes.update({'freesound_access_token': '', 'freesound_refresh_token': '', 'freesound_expires_at': 0})
        settings.update(changes)
        return settings.public()

    @app.post('/api/settings/text-models')
    def text_models(body: TextModels):
        configuration = settings.get()
        base = providers.validate_text_api_base(body.text_api_base if body.text_api_base is not None else configuration['text_api_base'])
        supplied_key = (body.text_api_key or '').strip()
        if base != configuration['text_api_base'].rstrip('/'):
            # Never forward a saved provider secret to a newly entered service.
            configuration['text_api_key'] = ''
        configuration['text_api_base'] = base
        if supplied_key:
            configuration['text_api_key'] = supplied_key
        return providers.list_text_models(configuration)

    @app.post('/api/settings/test')
    def test_settings(body: ConnectionTest):
        return providers.test_connection(body.provider, settings.get())

    @app.get('/api/freesound/authorize')
    def authorize_url():
        return {'url': providers.freesound_authorize_url(settings.get())}

    @app.post('/api/freesound/authorize')
    def authorize(body: Authorize):
        configuration = settings.get()
        updates = providers.freesound_exchange_code(body.code.strip(), configuration)
        with settings.lock:
            if any(settings.get()[k] != configuration[k] for k in ('freesound_key', 'freesound_client_id')):
                raise ValueError('授权期间 API 配置发生变化，请使用当前配置重新授权。')
            settings.update(updates)
        return {'ok': True}

    @app.post('/api/generate')
    def generate(body: Generate):
        if not body.prompt.strip():
            raise ValueError('请先写下你想要的音效描述。')
        configuration = settings.get()
        if not configuration['elevenlabs_key']:
            raise ValueError('先在设置中填写 ElevenLabs API Key，即可生成音效。')
        library.check_folder(body.folder_id)
        job_id = uuid.uuid4().hex
        with jobs_lock:
            if any(job['status'] in {'queued', 'running'} for job in jobs.values()):
                raise HTTPException(409, '已有一条音效正在生成，请等待完成后再提交，避免重复计费。')
            if len(jobs) >= 200:
                jobs.pop(next(iter(jobs)))
            jobs[job_id] = {'id': job_id, 'status': 'queued'}

        def worker():
            path = None
            with jobs_lock:
                jobs[job_id]['status'] = 'running'
            try:
                audio, ext = providers.generate_elevenlabs(body.prompt.strip(), body.duration, body.loop, configuration)
                extension = '.' + ext.lstrip('.').lower()
                if extension not in ALLOWED_EXTENSIONS or not audio or len(audio) > MAX_BYTES:
                    raise ValueError('生成服务返回了无效的音频格式或超大文件。')
                sound_id = uuid.uuid4().hex
                path = library.media / (sound_id + extension)
                path.write_bytes(audio)
                analysis = inspect_audio(path)
                sound = base_sound(sound_id, body.prompt.strip()[:70], 'elevenlabs', extension) | analysis
                license_value = {'unknown': '待确认', 'personal': 'Personal', 'commercial': 'Commercial'}[configuration['elevenlabs_license']]
                sound.update({'description': body.prompt.strip(), 'note': '循环音效' if body.loop else '',
                              'tags': ['AI生成', 'loop'] if body.loop else ['AI生成'],
                              'quality': 'generated', 'license': license_value, 'author': 'ElevenLabs / 用户生成',
                              'license_url': 'https://elevenlabs.io/terms-of-use',
                              'source_url': 'https://elevenlabs.io/sound-effects', 'folder_id': body.folder_id})
                # A folder can be deleted while a generation is running. Keep the result safely unfiled.
                with library.lock:
                    if body.folder_id not in {f['id'] for f in library.folders()}:
                        sound['folder_id'] = None
                    library.put(sound, path)
                with jobs_lock:
                    jobs[job_id] = {'id': job_id, 'status': 'complete', 'sound': sound}
            except Exception as exc:
                # Provider errors are already sanitized. Never echo arbitrary request/credential objects.
                message = str(exc) if isinstance(exc, (providers.ProviderError, ValueError)) else '生成过程中出现本机处理错误，请检查磁盘空间和 FFmpeg。'
                with jobs_lock:
                    jobs[job_id] = {'id': job_id, 'status': 'failed', 'error': message + ' 本次未自动重试；如需重试，请先确认服务商用量。'}

        threading.Thread(target=worker, name='soundbox-generation', daemon=True).start()
        return {'id': job_id, 'status': 'queued'}

    @app.get('/api/jobs/{job_id}')
    def job_status(job_id: str):
        with jobs_lock:
            if job_id not in jobs:
                raise HTTPException(404, '任务记录不存在或服务已重启。请先查看素材库，避免重复生成。')
            return jobs[job_id].copy()

    @app.get('/{resource:path}')
    def web(resource: str):
        if resource.startswith('api/'):
            raise HTTPException(404, '接口不存在。')
        dist = (ROOT / 'web' / 'dist').resolve()
        path = (dist / resource).resolve()
        if not path.is_relative_to(dist):
            raise HTTPException(404)
        if path.is_file():
            return FileResponse(path)
        if '.' in Path(resource).name:
            raise HTTPException(404)
        if not (dist / 'index.html').is_file():
            return JSONResponse({'detail': '界面文件尚未构建。维护者请在 web 目录运行 npm run build。'}, status_code=503)
        return FileResponse(dist / 'index.html', headers={'Cache-Control': 'no-cache'})

    return app


app = create_app()
