"""Conservative local measurements and non-destructive linear audition gain.

No score here claims perceptual quality, noise cleanliness, or certified true
peak. Only digital zero or a confirmed decoding failure is marked unusable.
Resource/tool failures raise ValueError and must remain untested in the UI.
Callers must resolve paths from their library, never accept client file paths.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading

from .audio import MAX_BYTES

VERSION = 1
MAX_SECONDS = 600
MAX_CHANNELS = 8
MAX_SAMPLE_RATE = 192000
PROBE_TIMEOUT = 15
ANALYSIS_TIMEOUT = 60
RENDER_TIMEOUT = 60
MAX_LOG_BYTES = 512 * 1024
PEAK_CEILING_DB = -2.0
MAX_GAIN_DB = 6.0


def _run(command: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    """Bound execution and both captured streams; never expose FFmpeg file names."""
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            shell=False, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
    except OSError:
        raise ValueError('无法启动音频检查，请确认 FFmpeg 和 ffprobe 已安装。') from None
    oversized = threading.Event()
    outputs: list[bytearray] = [bytearray(), bytearray()]

    def read_pipe(stream, output):
        try:
            while chunk := stream.read(16384):
                if len(output) + len(chunk) > MAX_LOG_BYTES:
                    oversized.set()
                    process.kill()
                    break
                output.extend(chunk)
        finally:
            stream.close()

    readers = [threading.Thread(target=read_pipe, args=(stream, output), daemon=True)
               for stream, output in zip((process.stdout, process.stderr), outputs)]
    for thread in readers:
        thread.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise ValueError('音频检查或处理超时，素材尚未判定，请缩短音频后重试。') from None
    finally:
        for thread in readers:
            thread.join()
    if oversized.is_set():
        raise ValueError('音频检查输出超过处理预算，素材尚未判定。')
    return process.returncode, bytes(outputs[0]), bytes(outputs[1])


def _tools() -> tuple[str, str]:
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        raise ValueError('音频检查需要 FFmpeg 和 ffprobe，请安装后重试。')
    return ffmpeg, ffprobe


def _source(path: Path) -> Path:
    if not isinstance(path, Path):
        raise ValueError('音频检查必须使用素材库提供的本机文件。')
    try:
        path = path.resolve(strict=True)
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise ValueError('音频超过 50 MB 检查预算或不是文件，素材尚未判定。')
    except OSError:
        raise ValueError('音频文件无法读取，素材尚未判定。') from None
    return path


def _probe(path: Path, ffprobe: str) -> dict | None:
    code, raw, _ = _run([
        ffprobe, '-v', 'error', '-protocol_whitelist', 'file,pipe',
        '-show_entries', 'format=duration:stream=codec_type,codec_name,channels,sample_rate,duration',
        '-of', 'json', str(path),
    ], PROBE_TIMEOUT)
    if code:
        return None
    try:
        metadata = json.loads(raw)
        stream = next(item for item in metadata.get('streams', []) if item.get('codec_type') == 'audio')
    except StopIteration:
        return None
    except (ValueError, TypeError, AttributeError):
        raise ValueError('无法读取音频检测信息，素材尚未判定。') from None
    try:
        channels = int(stream['channels'])
        sample_rate = int(stream['sample_rate'])
        duration = float(stream.get('duration') or metadata.get('format', {}).get('duration'))
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError('无法确认音频时长和声道，素材尚未判定。') from None
    if not math.isfinite(duration) or duration <= 0:
        return None
    if duration > MAX_SECONDS + .1 or not 1 <= channels <= MAX_CHANNELS or not 1 <= sample_rate <= MAX_SAMPLE_RATE:
        raise ValueError('音频超出检查预算（10 分钟、8 声道、192 kHz），素材尚未判定。')
    return {'channels': channels, 'sample_rate': sample_rate, 'duration': duration,
            'codec': stream.get('codec_name')}


def _empty(flag: str, message: str, duration: float = 0) -> dict:
    return {'version': VERSION, 'status': 'unusable', 'flags': [flag],
            'peak_dbfs': None, 'rms_dbfs': None, 'estimated_peak_dbfs': None,
            'leading_seconds': 0.0, 'trailing_seconds': 0.0,
            'duration': duration, 'gain_db': None, 'message': message}


def _overall(log: str, filter_index: int) -> dict[str, float]:
    result: dict[str, float] = {}
    in_overall = False
    expression = re.compile(r'\[Parsed_astats_' + str(filter_index) + r' @ [^]]+\]\s*(.*)')
    for line in log.splitlines():
        match = expression.search(line)
        if not match:
            continue
        content = match[1]
        if content == 'Overall':
            in_overall = True
        elif in_overall and ': ' in content:
            key, value = content.split(': ', 1)
            try:
                result[key] = float(value)
            except ValueError:
                pass
    return result


def analyze_audio(path: Path) -> dict:
    """Decode the complete first audio stream, retaining its channels and rate.

    Edges use an amplitude threshold of -50 dBFS lasting at least 100 ms, not a
    noise classifier. The second astats pass follows 192 kHz resampling and is
    only an inter-sample peak estimate. No audio or diagnostic text is persisted.
    """
    path = _source(path)
    ffmpeg, ffprobe = _tools()
    probe = _probe(path, ffprobe)
    if probe is None:
        return _empty('decode_error', '无法解码有效音频，请重新导入或检查源文件。')
    # -t permits detecting overflow, rather than silently passing a truncated
    # first 600 seconds. Metadata and actual decoded sample count both enforce it.
    filters = ('astats=metadata=0:reset=0,silencedetect=noise=-50dB:d=0.1,'
               'aresample=192000,astats=metadata=0:reset=0')
    code, _, stderr = _run([
        ffmpeg, '-nostdin', '-hide_banner', '-nostats', '-v', 'info', '-xerror',
        '-protocol_whitelist', 'file,pipe', '-i', str(path), '-map', '0:a:0', '-vn',
        '-t', str(MAX_SECONDS + .1), '-af', filters, '-f', 'null', '-',
    ], ANALYSIS_TIMEOUT)
    if code:
        return _empty('decode_error', '音频无法完整解码，请检查源文件。')
    log = stderr.decode('utf-8', errors='replace')
    native, oversampled = _overall(log, 0), _overall(log, 3)
    try:
        frames = native['Number of samples']
        duration = frames / probe['sample_rate']
        peak, rms = native['Peak level dB'], native['RMS level dB']
        estimated = max(peak, oversampled['Peak level dB'])
    except KeyError:
        # Unknown FFmpeg output isn't evidence of a bad recording.
        raise ValueError('未取得完整检测指标，素材尚未判定，请确认 FFmpeg 版本。') from None
    if not math.isfinite(duration) or duration <= 0:
        return _empty('decode_error', '音频没有可播放的数据。')
    if duration > MAX_SECONDS + 1 / probe['sample_rate']:
        raise ValueError('解码音频超过 10 分钟检查预算，素材尚未判定。')
    if peak == -math.inf:
        return _empty('silent', '检测到数字全静音；低音量素材不会因此被隐藏。', round(duration, 6))
    if not all(math.isfinite(value) for value in (peak, rms, estimated)):
        return _empty('decode_error', '解码音频包含无效采样，无法可靠播放。', round(duration, 6))

    segments: list[tuple[float, float]] = []
    start = None
    for line in log.splitlines():
        if not re.search(r'\[(?:Parsed_silencedetect_1|silencedetect) @ ', line):
            continue
        match = re.search(r'silence_start: ([\d.eE+-]+)', line)
        if match:
            start = max(0.0, float(match[1]))
        match = re.search(r'silence_end: ([\d.eE+-]+)', line)
        if match and start is not None:
            segments.append((start, min(duration, float(match[1]))))
            start = None
    if start is not None:
        segments.append((start, duration))
    leading = next((end for start, end in segments if start <= 1 / probe['sample_rate']), 0.0)
    trailing = next((duration - start for start, end in reversed(segments)
                     if end >= duration - 1 / probe['sample_rate']), 0.0)
    flags = []
    if rms < -24 and estimated < -5:
        flags.append('quiet')
    # A whole quiet signal has no identifiable edge; don't invite removing it.
    if leading < duration - .001 and trailing < duration - .001 and leading + trailing >= max(.25, duration * .2):
        flags.append('long_edges')
    if estimated >= -1:
        flags.append('peak_risk')
    if peak - rms >= 18:
        flags.append('strong_transient')
    gain = min(MAX_GAIN_DB, max(0.0, -20.0 - rms), PEAK_CEILING_DB - estimated)
    return {'version': VERSION, 'status': 'ready', 'flags': flags,
            'peak_dbfs': round(peak, 4), 'rms_dbfs': round(rms, 4),
            'estimated_peak_dbfs': round(estimated, 4), 'leading_seconds': round(leading, 6),
            'trailing_seconds': round(trailing, 6), 'duration': round(duration, 6),
            'gain_db': round(gain, 4),
            'message': '检测为客观电平提示，不代表音质评分；过采样峰值为估计值，不是认证真峰值。'}


def render_compensated(source: Path, target: Path, analysis: dict | None = None) -> dict:
    """Create a new PCM16 WAV with bounded linear gain and recheck its peak.

    A supplied report is an API convenience only: fresh local analysis prevents
    forged/stale measurements from driving gain. No limiter, denoise, trim or
    channel mixing is applied. Returned fields describe the saved output plus
    applied_gain_db; original provenance remains the caller's responsibility.
    """
    source = _source(source)
    if not isinstance(target, Path) or target.is_symlink():
        raise ValueError('补偿版必须另存为独立的新 WAV 文件。')
    target = target.resolve()
    if source == target or target.exists() or target.suffix.lower() != '.wav' or not target.parent.is_dir():
        raise ValueError('补偿版必须另存为新 WAV 文件，不能覆盖已有文件。')
    report = analyze_audio(source)
    if report['status'] != 'ready':
        raise ValueError('这条音频没有可补偿的有效声音。')
    ffmpeg, ffprobe = _tools()
    probe = _probe(source, ffprobe)
    if probe is None:
        raise ValueError('源音频无法读取，请重试。')
    predicted_bytes = math.ceil(report['duration'] * probe['sample_rate']) * probe['channels'] * 2 + 4096
    if predicted_bytes > MAX_BYTES:
        raise ValueError('补偿版 WAV 会超过 50 MB，请先缩短音频。')
    gain = report['gain_db']
    created = False
    try:
        # Atomic reservation ensures another request or an existing file is not
        # overwritten. On failure only the file reserved here is removed.
        try:
            with target.open('xb'):
                pass
            created = True
        except OSError:
            raise ValueError('无法创建补偿版，请检查目录权限或换一个文件名。') from None
        for attempt in range(3):
            code, _, _ = _run([
                ffmpeg, '-nostdin', '-hide_banner', '-v', 'error', '-y', '-xerror',
                '-protocol_whitelist', 'file,pipe', '-i', str(source), '-map', '0:a:0', '-vn',
                '-map_metadata', '-1', '-t', str(MAX_SECONDS + .1), '-af', f'volume={gain:.8f}dB',
                '-ar', str(probe['sample_rate']), '-ac', str(probe['channels']),
                '-c:a', 'pcm_s16le', '-f', 'wav', '-fs', str(MAX_BYTES + 1), str(target),
            ], RENDER_TIMEOUT)
            if code or not target.is_file() or not 0 < target.stat().st_size <= MAX_BYTES:
                raise ValueError('补偿版生成失败或超过 50 MB，原素材已保留。')
            output = analyze_audio(target)
            if output['status'] != 'ready' or abs(output['duration'] - report['duration']) > 2 / probe['sample_rate']:
                raise ValueError('补偿版未通过完整性检查，原素材已保留。')
            # Quantization or resampling can alter peaks slightly; re-render from
            # the untouched source with more headroom, never from the prior copy.
            overshoot = output['estimated_peak_dbfs'] - PEAK_CEILING_DB
            if overshoot <= .001:
                return output | {'applied_gain_db': round(gain, 4)}
            gain -= overshoot + .01
        raise ValueError('补偿版峰值仍超出保守上限，已停止保存并保留原素材。')
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise
