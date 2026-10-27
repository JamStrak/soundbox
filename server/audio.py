"""Audio validation, real waveform extraction and clearly labelled synthesized examples."""
from __future__ import annotations

import array
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import wave

from .storage import Library, now

ALLOWED_EXTENSIONS = {'.wav', '.mp3', '.ogg', '.flac', '.m4a', '.aac'}
MAX_BYTES = 50 * 1024 * 1024


def run_audio(command: list[str], timeout=40) -> bytes:
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError('无法读取音频：请确认 FFmpeg 已安装，且文件没有损坏或过长。') from exc
    if result.returncode:
        raise ValueError('这个文件无法作为音频读取，请选择有效的 WAV、MP3、OGG、FLAC 或 M4A 文件。')
    return result.stdout


def waveform(samples, points=96) -> list[float]:
    if not samples:
        return []
    step = max(1, len(samples) // points)
    peaks = [max((abs(v) for v in samples[i:i + step]), default=0)
             for i in range(0, len(samples), step)][:points]
    maximum = max(peaks, default=1) or 1
    return [round(min(1, p / maximum), 4) for p in peaks]


def inspect_audio(path: Path) -> dict:
    ffprobe = shutil.which('ffprobe')
    ffmpeg = shutil.which('ffmpeg')
    if not ffprobe or not ffmpeg:
        if path.suffix.lower() != '.wav':
            raise ValueError('当前缺少 FFmpeg，暂时只能导入 PCM WAV。请安装 FFmpeg 后重试其他格式。')
        try:
            with wave.open(str(path), 'rb') as stream:
                duration = stream.getnframes() / stream.getframerate()
                if stream.getsampwidth() != 2 or not 0 < duration <= 600:
                    raise ValueError('请选择 10 分钟以内的 16 位 PCM WAV 音效。')
                samples = array.array('h', stream.readframes(stream.getnframes()))
                if sys.byteorder != 'little':
                    samples.byteswap()
                return {'duration': round(duration, 3), 'waveform': waveform(samples)}
        except (wave.Error, EOFError) as exc:
            raise ValueError('WAV 音频无效或文件损坏。') from exc
    metadata = json.loads(run_audio([ffprobe, '-v', 'error', '-protocol_whitelist', 'file,pipe',
                                    '-show_entries', 'format=duration:stream=codec_type', '-of', 'json', str(path)]))
    try:
        duration = float(metadata['format']['duration'])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError('无法读取这条音频的时长。请先导出为 WAV 或 MP3 后导入。') from exc
    if not any(s.get('codec_type') == 'audio' for s in metadata.get('streams', [])):
        raise ValueError('文件中没有有效音频。')
    if not math.isfinite(duration) or not 0 < duration <= 600:
        raise ValueError('第一版支持 10 分钟以内的音效，请先裁剪较长音频。')
    raw = run_audio([ffmpeg, '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-i', str(path),
                     '-vn', '-ac', '1', '-ar', '8000', '-t', '600', '-f', 'f32le', 'pipe:1'])
    samples = array.array('f', raw)
    if sys.byteorder != 'little':
        samples.byteswap()
    if not samples:
        raise ValueError('音频没有可播放的数据。')
    return {'duration': round(duration, 3), 'waveform': waveform(samples)}


@lru_cache(maxsize=256)
def _cached_browser_probe(path: str, mtime_ns: int, size: int, ffprobe: str) -> dict:
    # The stat fingerprint invalidates cached metadata when downloads replace a file.
    return json.loads(run_audio([
        ffprobe, '-v', 'error', '-protocol_whitelist', 'file,pipe',
        '-show_entries', 'format=format_name:stream=codec_type,codec_name,channels',
        '-of', 'json', path,
    ], timeout=12))


def _browser_probe(path: Path) -> dict:
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        return {}
    try:
        resolved = path.resolve()
        info = resolved.stat()
        return _cached_browser_probe(str(resolved), info.st_mtime_ns, info.st_size, ffprobe)
    except (OSError, ValueError, TypeError):
        return {}


def _direct_browser_mime(metadata: dict) -> str | None:
    streams = [stream for stream in metadata.get('streams', []) if stream.get('codec_type') == 'audio']
    if len(streams) != 1 or streams[0].get('channels') not in (1, 2):
        return None
    container = metadata.get('format', {}).get('format_name')
    codec = streams[0].get('codec_name')
    return {('mp3', 'mp3'): 'audio/mpeg', ('ogg', 'vorbis'): 'audio/ogg',
            ('wav', 'pcm_s16le'): 'audio/wav'}.get((container, codec))


def browser_audio_path(path: Path) -> Path:
    """Choose an existing playback file; never let a legacy cache mask direct audio.

    Imports/downloads call make_browser_preview for unsupported encodings. This
    selector does not start an encoder during an HTTP range request.
    """
    if _direct_browser_mime(_browser_probe(path)):
        return path
    preview = path.with_name(path.stem + '.listen.mp3')
    return preview if preview.is_file() else path


def browser_audio_mime(path: Path) -> str:
    """MIME for the selected file, determined from actual bytes, not its suffix."""
    metadata = _browser_probe(path)
    direct = _direct_browser_mime(metadata)
    if direct:
        return direct
    return {'mp3': 'audio/mpeg', 'ogg': 'audio/ogg', 'wav': 'audio/wav',
            'flac': 'audio/flac', 'aac': 'audio/aac',
            'mov,mp4,m4a,3gp,3g2,mj2': 'audio/mp4'}.get(
                metadata.get('format', {}).get('format_name'), 'application/octet-stream')


def make_browser_preview(path: Path):
    """Transcode unsupported encodings only; verified browser formats stay intact."""
    if _direct_browser_mime(_browser_probe(path)):
        return
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg:
        target = path.with_name(path.stem + '.listen.mp3')
        try:
            run_audio([ffmpeg, '-nostdin', '-v', 'error', '-y', '-protocol_whitelist', 'file,pipe',
                       '-i', str(path), '-map', '0:a:0', '-vn', '-ac', '2', '-ar', '44100',
                       '-codec:a', 'libmp3lame', '-b:a', '128k', str(target)])
        except ValueError:
            target.unlink(missing_ok=True)


def base_sound(sound_id: str, name: str, source: str, ext='wav') -> dict:
    return {'id': sound_id, 'name': name, 'tags': [], 'description': '', 'duration': 0,
            'source': source, 'license': '待确认', 'license_url': '', 'author': '', 'source_url': '',
            'preview_url': f'/api/audio/{sound_id}', 'waveform': [], 'favorite': False,
            'folder_id': None, 'saved': True, 'downloaded': True, 'format': ext.lstrip('.'),
            'created_at': now(), 'note': '', 'quality': 'original'}


SEEDS = [
    ('ui-click', '界面轻触 · UI Click', .12, ['界面', '按钮', '点击', 'ui', 'button', 'click']),
    ('confirm', '确认提示 · Confirm', .38, ['界面', '确认', '成功', 'ui', 'confirm', 'success']),
    ('cancel', '取消提示 · Cancel', .3, ['界面', '取消', '返回', 'ui', 'cancel', 'back']),
    ('coin', '金币拾取 · Coin Pickup', .6, ['界面', '金币', '拾取', 'coin', 'pickup', 'collect']),
    ('level-up', '升级提示 · Level Up', 1.2, ['界面', '升级', '奖励', 'level', 'up', 'reward']),
    ('magic', '魔法闪光 · Magic Sparkle', 1.8, ['魔法', '闪光', '技能', 'magic', 'sparkle', 'spell']),
    ('laser', '激光射击 · Laser Shot', .65, ['战斗', '激光', '射击', 'combat', 'laser', 'shot']),
    ('sword', '剑刃挥动 · Sword Whoosh', .55, ['战斗', '刀剑', '挥动', '挥剑', 'combat', 'sword', 'whoosh', 'swing']),
    ('metal', '金属撞击 · Metal Impact', 1.1, ['战斗', '金属', '碰撞', '打铁', '盾牌', 'combat', 'metal', 'impact', 'shield']),
    ('explosion', '低频爆炸 · Explosion', 1.7, ['战斗', '爆炸', '冲击', 'combat', 'explosion', 'boom']),
    ('footsteps', '石地脚步 · Stone Footsteps', 2.4, ['脚步', '行走', '石头', 'footsteps', 'walk', 'stone']),
    ('rain', '细雨环境 · Rain Ambience', 6.0, ['环境', '下雨', '雨声', '循环', 'ambience', 'rain', 'loop']),
]


def synthesize(kind: str, seconds: float, sample_rate=44100):
    rng = random.Random(kind)
    samples = array.array('h')
    filtered = 0.0
    for i in range(int(seconds * sample_rate)):
        t = i / sample_rate
        p = t / seconds
        noise = rng.uniform(-1, 1)
        filtered = .93 * filtered + .07 * noise
        if kind == 'ui-click':
            v = (math.sin(2 * math.pi * 1600 * t) * .5 + noise * .12) * math.exp(-t * 65)
        elif kind in ('confirm', 'cancel', 'coin', 'level-up'):
            notes = {'confirm': [660, 990], 'cancel': [540, 330], 'coin': [1046, 1568, 2093],
                     'level-up': [523, 659, 784, 1046]}[kind]
            index = min(len(notes) - 1, int(p * len(notes)))
            phase = t - index * seconds / len(notes)
            v = (.45 * math.sin(2 * math.pi * notes[index] * t) + .1 * math.sin(4 * math.pi * notes[index] * t)) * math.exp(-phase * 15)
        elif kind == 'magic':
            v = sum(math.sin(2 * math.pi * f * t) * math.exp(-max(0, t - d) * 5) * (t >= d)
                    for f, d in [(880, 0), (1320, .2), (1760, .4), (2200, .6)]) * .16
        elif kind == 'laser':
            v = .6 * math.sin(2 * math.pi * (1500 * t - 1000 * t * t)) * math.exp(-t * 8)
        elif kind == 'sword':
            v = (noise - filtered) * .55 * math.sin(math.pi * p) ** 3
        elif kind == 'metal':
            v = sum(math.sin(2 * math.pi * f * t) for f in [410, 683, 1101, 1829]) * .15 * math.exp(-t * 7) + noise * .1 * math.exp(-t * 70)
        elif kind == 'explosion':
            v = (filtered * 2 + math.sin(2 * math.pi * (62 * t - 12 * t * t)) * .24) * math.exp(-t * 3.3)
        elif kind == 'footsteps':
            phase = t % .48
            v = (noise * .3 + math.sin(2 * math.pi * 110 * phase) * .45) * math.exp(-phase * 35)
        else:
            v = (noise * .2 + filtered * .9) * (.7 + .1 * math.sin(2 * math.pi * t / seconds))
        fade = min(1, t / .004, (seconds - t) / .035)
        samples.append(int(max(-.95, min(.95, v * fade)) * 32767))
    return samples


def seed_library(library: Library):
    if library.seeded():
        return
    for kind, name, duration, tags in SEEDS:
        sound_id = 'builtin-' + kind
        if library.get(sound_id):
            continue
        path = library.media / (sound_id + '.wav')
        samples = synthesize(kind, duration)
        with wave.open(str(path), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(44100)
            packed = array.array('h', samples)
            if sys.byteorder != 'little':
                packed.byteswap()
            output.writeframes(packed.tobytes())
        sound = base_sound(sound_id, name, 'builtin') | {
            'tags': tags, 'duration': duration, 'waveform': waveform(samples), 'license': 'CC0',
            'license_url': 'https://creativecommons.org/publicdomain/zero/1.0/',
            'author': '声匣 · 程序合成',
            'description': '随应用提供的程序合成示例，用于体验试听和素材管理。可自由用于游戏项目；不是在线检索或 AI 生成结果。',
        }
        library.put(sound, path)
    library.mark_seeded()
