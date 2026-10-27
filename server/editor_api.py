"""Local, non-destructive audio editing drafts and library exports."""
from __future__ import annotations

from copy import deepcopy
from contextlib import ExitStack
from pathlib import Path
import re
import shutil
import threading
import time
import uuid
from typing import Literal

from fastapi import HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from . import editing
from .audio import base_sound


class EditorModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Clip(EditorModel):
    sound_id: str = Field(min_length=1, max_length=100)
    start: float = Field(default=0, ge=0, le=600)
    end: float = Field(gt=0, le=600)
    gain_db: float = Field(default=0, ge=-60, le=12)
    fade_in: float = Field(default=0, ge=0, le=240)
    fade_out: float = Field(default=0, ge=0, le=240)
    fade_in_curve: Literal['linear', 'slow_fast', 'fast_slow', 's_curve'] = 'linear'
    fade_out_curve: Literal['linear', 'slow_fast', 'fast_slow', 's_curve'] = 'linear'


class Render(EditorModel):
    clips: list[Clip] = Field(min_length=1, max_length=12)
    master_gain_db: float = Field(default=0, ge=-24, le=12)
    gap: float = Field(default=0, ge=0, le=5)
    format: str = Field(default='wav', pattern='^(wav|mp3)$')


class SaveEdit(EditorModel):
    preview_id: str = Field(pattern='^[0-9a-f]{32}$')
    name: str = Field(min_length=1, max_length=200)
    folder_id: str | None = None


def provenance(sounds: list[dict]) -> dict:
    """Carry source licenses forward; never relicense a composition as CC0."""
    origins, seen = [], set()
    fields = ('id', 'name', 'author', 'license', 'license_url', 'source', 'origin_source', 'source_url', 'attribution', 'quality', 'original_filename')
    for sound in sounds:
        original_sources = sound.get('edit_sources') or [sound]
        for item in original_sources:
            item = {key: str(item.get(key) or '') for key in fields}
            key = tuple(item[key] for key in ('id', 'license', 'license_url'))
            if key not in seen:
                origins.append(item)
                seen.add(key)
    if len(origins) > 100:
        raise ValueError('这个拼接包含超过 100 个原始来源，请拆分为更小的作品。')
    licenses = {(item['license'], item['license_url']) for item in origins}
    all_cc0 = all(item['license'].lower() in {'cc0', 'cc0 1.0'} for item in origins)
    if all_cc0:
        license_name, license_url = 'CC0', 'https://creativecommons.org/publicdomain/zero/1.0/'
    elif len(licenses) == 1:
        license_name, license_url = next(iter(licenses))
    else:
        license_name, license_url = '混合许可 · 需核对', ''
    attribution = '\n'.join(
        ' | '.join(filter(None, [item['name'], item['author'], item['license'], item['license_url'],
                                item['source_url'], item['attribution']])) for item in origins)
    return {'license': license_name or '待确认', 'license_url': license_url,
            'author': ' / '.join(dict.fromkeys(item['author'] for item in origins if item['author'])),
            'source_url': origins[0]['source_url'] if len(origins) == 1 else '',
            'attribution': '在声匣中裁剪、调节或拼接；原始来源：\n' + attribution,
            'origin_source': ' / '.join(dict.fromkeys(item['origin_source'] or item['source'] for item in origins)),
            'edit_sources': origins}


def register_editor(app, library, public_sound, video_sources=None):
    directory = library.directory / 'editor-previews'
    directory.mkdir(exist_ok=True)
    previews: dict[str, dict] = {}
    lock = threading.RLock()
    render_slot = threading.BoundedSemaphore(1)
    lifetime = 60 * 60
    app.state.editor_previews = previews

    def remove_file(path: Path):
        resolved = path.resolve()
        if resolved.parent == directory.resolve() and re.fullmatch(r'[0-9a-f]{32}\.(wav|mp3)', resolved.name):
            try:
                resolved.unlink(missing_ok=True)
                return True
            except PermissionError:
                # Windows may still have a Range response/player handle open.
                # Keep the cleanup record and retry after playback releases it.
                return False
        return False

    def cleanup():
        expired = time.time() - lifetime
        for identifier, entry in list(previews.items()):
            if entry['created'] < expired or entry.get('discarded'):
                if remove_file(entry['path']):
                    del previews[identifier]
        # Also retire abandoned previews from an earlier server instance.
        for path in directory.iterdir():
            if path.is_file() and path.stat().st_mtime < expired:
                remove_file(path)
        for identifier, entry in list(previews.items()):
            if len(previews) < 8:
                break
            if remove_file(entry['path']):
                del previews[identifier]
        if len(previews) >= 8:
            raise HTTPException(409, '多个试听文件仍在播放，请停止试听后重新生成。')

    def get_preview(identifier):
        entry = previews.get(identifier)
        if entry is None or entry.get('discarded') or entry['created'] < time.time() - lifetime:
            raise HTTPException(404, '试听草稿已过期或服务已重启，请重新生成试听。')
        return entry

    def export_filename(value: str) -> str:
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip(' .')[:140] or '声匣音效'
        if Path(value).suffix.lower() in {'.wav', '.mp3', '.ogg', '.flac', '.m4a', '.aac'}:
            value = value[:-len(Path(value).suffix)].strip(' .') or '声匣音效'
        if value.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))}:
            value = '_' + value
        return value + '.wav'

    @app.post('/api/editor/render')
    def render(body: Render):
        if not render_slot.acquire(blocking=False):
            raise HTTPException(409, '已有音频正在处理，请等它完成后再试听。')
        target = None
        source_leases = ExitStack()
        try:
            clips, sources = [], []
            for clip in body.clips:
                if video_sources is not None and clip.sound_id.startswith('video-'):
                    sound, path = source_leases.enter_context(video_sources.lease(clip.sound_id))
                else:
                    sound = library.get(clip.sound_id)
                    path = library.file(clip.sound_id)
                if sound is None or path is None:
                    raise HTTPException(400, '有音效尚未下载或已移除，请先保存音频到素材库再剪辑。')
                clips.append(clip.model_dump(exclude={'sound_id'}) | {'path': path})
                sources.append(sound)
            metadata = provenance(sources)
            identifier = uuid.uuid4().hex
            target = directory / f'{identifier}.{body.format}'
            with lock:
                cleanup()
            analysis = editing.render_audio(clips, target, output_format=body.format,
                                            master_gain_db=body.master_gain_db, gap=body.gap)
            response = {'id': identifier, 'preview_url': f'/api/editor/previews/{identifier}',
                        'duration': analysis['duration'], 'waveform': analysis['waveform'],
                        'format': body.format, 'warnings': analysis.get('warnings', [])}
            with lock:
                previews[identifier] = {'path': target, 'created': time.time(), 'response': response,
                                        'recipe': body.model_dump(), 'provenance': metadata,
                                        'tags': list(dict.fromkeys(t for s in sources for t in s.get('tags', [])))[:39] + ['剪辑'],
                                        'saved_id': None}
            return response
        except (HTTPException, ValueError):
            if target:
                remove_file(target)
            raise
        except OSError:
            if target:
                remove_file(target)
            raise HTTPException(500, '无法写入剪辑音频，请检查磁盘空间与文件夹权限。') from None
        except Exception:
            if target:
                remove_file(target)
            raise HTTPException(500, '本机音频处理未完成，请重新生成试听。') from None
        finally:
            try:
                source_leases.close()
            finally:
                render_slot.release()

    @app.get('/api/editor/previews/{preview_id}')
    def preview_audio(preview_id: str):
        with lock:
            entry = get_preview(preview_id)
            if not entry['path'].is_file():
                raise HTTPException(404, '试听文件已清理，请重新生成。')
            return FileResponse(entry['path'])

    @app.get('/api/editor/previews/{preview_id}/download')
    def download_preview(preview_id: str, name: str = Query(default='声匣音效', max_length=200)):
        with lock:
            entry = get_preview(preview_id)
            if entry['response']['format'] != 'wav':
                raise HTTPException(400, '导出到 DAW 请选择 WAV 格式，并重新生成试听。')
            if not entry['path'].is_file():
                raise HTTPException(404, '试听文件已清理，请重新生成。')
            return FileResponse(entry['path'], media_type='audio/wav', filename=export_filename(name))

    @app.delete('/api/editor/previews/{preview_id}')
    def delete_preview(preview_id: str):
        with lock:
            entry = previews.get(preview_id)
            if entry:
                if remove_file(entry['path']):
                    del previews[preview_id]
                else:
                    entry['discarded'] = True
        return {'ok': True}

    @app.post('/api/editor/save')
    def save_edit(body: SaveEdit):
        name = body.name.strip()
        if not name or any(ord(c) < 32 for c in name):
            raise ValueError('请输入有效的音效名称。')
        with lock, library.lock:
            entry = get_preview(body.preview_id)
            # Retrying after a lost response must not create duplicate exports.
            if entry['saved_id']:
                sound = library.get(entry['saved_id'])
                if sound:
                    return public_sound(sound)
                raise HTTPException(409, '这个剪辑结果已保存后移除，请重新生成试听。')
            library.check_folder(body.folder_id)
            identifier = 'edit-' + uuid.uuid4().hex
            target = library.media / (identifier + '.' + entry['response']['format'])
            try:
                shutil.copyfile(entry['path'], target)
                sound = base_sound(identifier, name, 'edited', target.suffix) | deepcopy(entry['provenance'])
                sound.update({key: entry['response'][key] for key in ('duration', 'waveform', 'format')})
                sound.update({'folder_id': body.folder_id, 'quality': 'edited', 'tags': entry['tags'],
                              'edit_recipe': deepcopy(entry['recipe']),
                              'description': f"本机剪辑：{len(entry['recipe']['clips'])} 个片段。原素材保留。"})
                library.put(sound, target)
            except OSError:
                target.unlink(missing_ok=True)
                raise HTTPException(500, '保存失败，请检查磁盘空间后重试；原素材未改动。') from None
            except Exception:
                target.unlink(missing_ok=True)
                raise HTTPException(500, '剪辑结果未能写入素材库，请重新保存。') from None
            entry['saved_id'] = identifier
            return public_sound(sound)
