"""Non-destructive audio trimming, gain, fades and ordered concatenation.

Input paths must be resolved from the library by the caller. FFmpeg is always
invoked without a shell, with file/pipe protocols only and a hidden window.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import subprocess

from .audio import ALLOWED_EXTENSIONS, MAX_BYTES, inspect_audio

SAMPLE_RATE = 44100
MAX_SECONDS = 240
MIN_CLIP_SECONDS = .02
MAX_CLIPS = 12
_FRAME_EPSILON = 1 / SAMPLE_RATE
# UI names describe progress through time, for both fade directions. FFmpeg
# instead evaluates its curve against a gain that runs backwards on fade-out.
_FADE_IN_CURVES = {"linear": "tri", "slow_fast": "qua", "fast_slow": "ipar", "s_curve": "hsin"}
_FADE_OUT_CURVES = {"linear": "tri", "slow_fast": "ipar", "fast_slow": "qua", "s_curve": "hsin"}


def _fade_curve(value, label: str, *, fade_out: bool = False) -> str:
    curves = _FADE_OUT_CURVES if fade_out else _FADE_IN_CURVES
    if not isinstance(value, str) or value not in curves:
        raise ValueError(f"{label}只支持直线、先慢后快、先快后慢或 S 形。")
    return curves[value]


def _number(value, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label}必须是有效数字。")
    number = float(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"{label}必须在 {minimum:g}–{maximum:g} 之间。")
    return number


def _run(command: list[str], timeout: int, operation: str) -> bytes:
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        raise ValueError(f"{operation}超时，请减少片段或缩短音频后重试。") from None
    except OSError:
        raise ValueError(f"{operation}无法启动，请确认 FFmpeg 和 ffprobe 已安装且可运行。") from None
    if result.returncode:
        raise ValueError(f"{operation}失败，音频可能损坏或编码不受支持；请重新导入有效音频后重试。")
    return result.stdout


def _duration(path: Path, ffprobe: str) -> float:
    raw = _run([
        ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe",
        "-show_entries", "format=duration:stream=codec_type,duration", "-of", "json", str(path),
    ], 25, "读取音频时长")
    try:
        metadata = json.loads(raw)
        stream = next(item for item in metadata.get("streams", []) if item.get("codec_type") == "audio")
        duration = float(stream.get("duration") or metadata.get("format", {}).get("duration"))
    except (ValueError, TypeError, KeyError, StopIteration, AttributeError):
        raise ValueError("无法确定源音频的实际时长，请重新导出为 WAV 或 MP3 后导入。") from None
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("源音频没有有效的可播放时长。")
    return duration


def _frames(seconds: float) -> int:
    return math.floor(seconds * SAMPLE_RATE + .5)


def render_audio(clips: list[dict], target: Path, output_format: str = "wav", master_gain_db: float = 0, gap: float = 0) -> dict:
    """Render to a new file and return its actual duration, waveform and warnings.

    A maximum 1 ms end-time rounding discrepancy is clamped to the real source
    duration because the library's displayed duration is rounded to milliseconds.
    All larger range errors are rejected before creating an output.
    """
    if not isinstance(clips, list) or not 1 <= len(clips) <= MAX_CLIPS:
        raise ValueError("一次编辑需要 1–12 个音频片段。")
    if not isinstance(target, Path):
        raise ValueError("输出必须是由本机素材库提供的新文件路径。")
    if not isinstance(output_format, str) or output_format.lower() not in {"wav", "mp3"}:
        raise ValueError("输出格式只支持 WAV 或 MP3。")
    output_format = output_format.lower()
    master_gain_db = _number(master_gain_db, "总音量增益", -24, 12)
    gap = _number(gap, "片段间隔", 0, 5)
    if target.is_symlink():
        raise ValueError("输出必须是独立的新文件，不能使用符号链接。")
    target = target.resolve()
    if target.suffix.lower() != "." + output_format:
        raise ValueError("输出文件扩展名必须与所选格式一致。")
    if target.exists():
        raise ValueError("输出文件已存在；编辑必须保存为新文件，不能覆盖原文件。")
    if not target.parent.is_dir():
        raise ValueError("输出目录不存在，请检查素材库目录后重试。")

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise ValueError("音频编辑需要 FFmpeg 和 ffprobe，请安装后重启声匣再试。")

    prepared = []
    durations: dict[Path, float] = {}
    total_frames = 0
    total_seconds = 0.0
    for index, clip in enumerate(clips, 1):
        if not isinstance(clip, dict) or not isinstance(clip.get("path"), Path):
            raise ValueError(f"第 {index} 个片段缺少有效的本地音频文件。")
        path = clip["path"].resolve()
        if path == target:
            raise ValueError("输出不能与源音频相同，请另存为新文件。")
        if not path.is_file() or path.suffix.lower() not in ALLOWED_EXTENSIONS | {".aif", ".aiff"}:
            raise ValueError(f"第 {index} 个源音频不存在或格式不受支持，请重新导入。")
        start = _number(clip.get("start", 0), f"第 {index} 段起点", 0, 86400)
        end = _number(clip.get("end"), f"第 {index} 段终点", 0, 86400)
        gain = _number(clip.get("gain_db", 0), f"第 {index} 段音量增益", -60, 12)
        fade_in = _number(clip.get("fade_in", 0), f"第 {index} 段淡入时长", 0, MAX_SECONDS)
        fade_out = _number(clip.get("fade_out", 0), f"第 {index} 段淡出时长", 0, MAX_SECONDS)
        in_curve = _fade_curve(clip.get("fade_in_curve", "linear"), f"第 {index} 段淡入曲线")
        out_curve = _fade_curve(clip.get("fade_out_curve", "linear"), f"第 {index} 段淡出曲线", fade_out=True)
        if end - start < MIN_CLIP_SECONDS - 1e-9:
            raise ValueError(f"第 {index} 段终点必须晚于起点，且至少保留 0.02 秒。")
        if path not in durations:
            durations[path] = _duration(path, ffprobe)
        actual = durations[path]
        if end > actual + .001:
            raise ValueError(f"第 {index} 段终点超出源音频实际时长（{actual:.3f} 秒）。")
        end = min(end, actual)
        if start >= actual or end - start < MIN_CLIP_SECONDS - _FRAME_EPSILON:
            raise ValueError(f"第 {index} 段超出源音频范围，或有效内容不足 0.02 秒。")
        length = end - start
        if fade_in > length + 1e-9 or fade_out > length + 1e-9 or fade_in + fade_out > length + 1e-9:
            raise ValueError(f"第 {index} 段淡入淡出各自及合计时长不能超过截取长度。")
        first_frame, last_frame = _frames(start), _frames(end)
        frame_count = last_frame - first_frame
        if frame_count < _frames(MIN_CLIP_SECONDS):
            raise ValueError(f"第 {index} 段实际采样长度不足 0.02 秒。")
        total_frames += frame_count
        total_seconds += length
        prepared.append((path, first_frame, last_frame, gain, min(_frames(fade_in), frame_count),
                         min(_frames(fade_out), frame_count), in_curve, out_curve))

    gap_frames = _frames(gap)
    total_frames += gap_frames * (len(clips) - 1)
    total_seconds += gap * (len(clips) - 1)
    if total_seconds > MAX_SECONDS + 1e-9 or total_frames > MAX_SECONDS * SAMPLE_RATE:
        raise ValueError("编辑后的总时长（含间隔）不能超过 240 秒，请减少或缩短片段。")
    # 16-bit stereo PCM is the largest supported output; MP3 is smaller.
    if output_format == "wav" and total_frames * 4 + 4096 > MAX_BYTES:
        raise ValueError("输出音频将超过 50 MB，请缩短片段或选择 MP3。")

    command = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y"]
    filters = []
    for index, (path, first, last, gain, fade_in, fade_out, in_curve, out_curve) in enumerate(prepared):
        command.extend(["-protocol_whitelist", "file,pipe", "-i", str(path)])
        length = last - first
        chain = (
            f"[{index}:a:0]aresample={SAMPLE_RATE},"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"asetpts=PTS-STARTPTS,atrim=start_sample={first}:end_sample={last},"
            f"asetpts=PTS-STARTPTS,volume={gain:g}dB"
        )
        if fade_in:
            chain += f",afade=t=in:ss=0:ns={fade_in}:curve={in_curve}"
        if fade_out:
            chain += f",afade=t=out:ss={length - fade_out}:ns={fade_out}:curve={out_curve}"
        if gap_frames and index < len(prepared) - 1:
            chain += f",apad=pad_len={gap_frames}"
        filters.append(chain + f"[clip{index}]")
    labels = "".join(f"[clip{index}]" for index in range(len(prepared)))
    filters.append(
        labels + f"concat=n={len(prepared)}:v=0:a=1,volume={master_gain_db:g}dB,"
        # The default alimiter 'level=true' would raise quiet audio to the limit.
        # Disable that behavior and compensate lookahead delay for exact cuts.
        f"alimiter=limit=0.95:level=false:latency=true,atrim=end_sample={total_frames}[edited]"
    )
    command.extend(["-filter_complex", ";".join(filters), "-map", "[edited]", "-vn", "-map_metadata", "-1", "-ar", str(SAMPLE_RATE), "-ac", "2"])
    if output_format == "wav":
        command.extend(["-c:a", "pcm_s16le", "-f", "wav"])
    else:
        command.extend(["-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3"])
    command.append(str(target))

    # Reserve the filename exclusively. '-y' may overwrite only this empty file
    # created by this invocation; existing targets and all source files are safe.
    created = False
    try:
        try:
            with target.open("xb"):
                pass
            created = True
        except OSError:
            raise ValueError("无法创建新的输出文件；请检查磁盘空间、目录权限或更换文件名。") from None
        _run(command, 120, "处理音频")
        if not target.is_file() or not 0 < target.stat().st_size <= MAX_BYTES:
            raise ValueError("生成的音频为空或超过 50 MB，已停止保存。")
        analysis = inspect_audio(target)
        if not analysis.get("waveform") or not math.isfinite(analysis.get("duration", 0)) or analysis["duration"] <= 0:
            raise ValueError("编辑后的音频无法正常播放，已停止保存。")
        # MP3 containers report encoder padding as up to one extra frame; the
        # decoded samples retain the exact trimmed duration. Allow only that pad.
        padding = .06 if output_format == "mp3" else .001
        if analysis["duration"] > MAX_SECONDS + padding:
            raise ValueError("生成的音频超过 240 秒，已停止保存。")
        warnings = []
        if master_gain_db > 0 or any(item[3] > 0 for item in prepared):
            warnings.append("已启用峰值过载保护；过大的音量增益会被限制，避免爆音。")
        return {"duration": analysis["duration"], "waveform": analysis["waveform"], "warnings": warnings}
    except Exception:
        if created:
            target.unlink(missing_ok=True)
        raise
