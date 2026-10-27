"""Real FFmpeg checks of audible behavior and non-destructive output handling."""
import array
import hashlib
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import wave

import pytest

from server import editing


@pytest.fixture(autouse=True)
def ffmpeg_available():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Real audio-editing regressions require FFmpeg and ffprobe")


def write_tones(path, segments, rate=44100, channels=1, amplitude=.25):
    samples = array.array("h")
    for frequency, duration in segments:
        for index in range(round(duration * rate)):
            value = round(amplitude * 32767 * math.sin(2 * math.pi * frequency * index / rate))
            samples.extend([value] * channels)
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(samples.tobytes())
    return path


def read_wav(path):
    with wave.open(str(path), "rb") as stream:
        info = stream.getparams()
        samples = array.array("h", stream.readframes(info.nframes))
    if sys.byteorder != "little":
        samples.byteswap()
    values = [samples[index] / 32768 for index in range(0, len(samples), info.nchannels)]
    return info, values


def rms(values):
    return math.sqrt(sum(value * value for value in values) / max(1, len(values)))


def window(values, start, end):
    return values[round(start * 44100):round(end * 44100)]


def tone_strength(values, frequency):
    real = sum(value * math.cos(2 * math.pi * frequency * index / 44100) for index, value in enumerate(values))
    imag = sum(value * math.sin(2 * math.pi * frequency * index / 44100) for index, value in enumerate(values))
    return math.hypot(real, imag) / len(values)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clip(path, start=0, end=1, **changes):
    return {"path": path, "start": start, "end": end, "gain_db": 0, "fade_in": 0, "fade_out": 0, **changes}


def test_trim_selects_correct_frequency_section_with_sample_accurate_duration(tmp_path):
    original = write_tones(tmp_path / "two sections.wav", [(440, .5), (880, .5)])
    before = digest(original)
    output = tmp_path / "selected.wav"
    result = editing.render_audio([clip(original, .6, .8)], output)
    info, values = read_wav(output)
    assert (info.nchannels, info.sampwidth, info.framerate) == (2, 2, 44100)
    assert info.nframes == 8820
    assert result["duration"] == pytest.approx(.2, abs=.001)
    assert len(result["waveform"]) == 96 and max(result["waveform"]) > 0
    assert tone_strength(values, 880) > tone_strength(values, 440) * 30
    assert digest(original) == before


def test_concat_resamples_channels_preserves_order_and_inserts_only_middle_gap(tmp_path):
    first = write_tones(tmp_path / "first.wav", [(440, .3)], rate=22050, channels=1)
    second = write_tones(tmp_path / "second.wav", [(880, .3)], rate=48000, channels=2)
    before = [digest(first), digest(second)]
    output = tmp_path / "combined.wav"
    result = editing.render_audio([clip(first, 0, .2), clip(second, 0, .15)], output, gap=.1)
    info, values = read_wav(output)
    assert info.nframes == round(.45 * 44100)
    assert result["duration"] == pytest.approx(.45, abs=.001)
    beginning = window(values, .03, .15)
    ending = window(values, .32, .42)
    assert tone_strength(beginning, 440) > tone_strength(beginning, 880) * 30
    assert rms(window(values, .22, .28)) < .0001
    assert tone_strength(ending, 880) > tone_strength(ending, 440) * 30
    assert rms(window(values, .43, .45)) > .05  # No extra silence after last clip.
    assert [digest(first), digest(second)] == before


@pytest.mark.parametrize(("gain", "master", "ratio"), [(-6, 0, 10 ** (-6 / 20)), (0, -6, 10 ** (-6 / 20)), (6, -6, 1), (6, 0, 10 ** (6 / 20))])
def test_clip_and_master_gain_change_rms_without_automatic_normalization(tmp_path, gain, master, ratio):
    source = write_tones(tmp_path / "quiet.wav", [(440, .5)], amplitude=.1)
    normal, adjusted = tmp_path / "normal.wav", tmp_path / "adjusted.wav"
    editing.render_audio([clip(source, end=.5)], normal)
    editing.render_audio([clip(source, end=.5, gain_db=gain)], adjusted, master_gain_db=master)
    _, baseline = read_wav(normal)
    _, changed = read_wav(adjusted)
    assert rms(window(changed, .05, .45)) / rms(window(baseline, .05, .45)) == pytest.approx(ratio, rel=.01)


def test_fades_have_audible_ramp_envelope_and_keep_middle_gain(tmp_path):
    source = write_tones(tmp_path / "constant tone.wav", [(440, 1)], channels=2)
    output = tmp_path / "faded.wav"
    editing.render_audio([clip(source, fade_in=.25, fade_out=.25)], output)
    _, values = read_wav(output)
    middle = rms(window(values, .4, .6))
    early = rms(window(values, .03, .05))
    later = rms(window(values, .15, .17))
    assert early < middle * .3
    assert early * 2 < later < middle * .8
    assert rms(window(values, .95, .97)) < middle * .3
    assert middle == pytest.approx(.25 / math.sqrt(2), rel=.01)
    assert abs(values[0]) < .001 and abs(values[-1]) < .001


def write_constant_level(path, duration=3):
    """A quiet DC signal makes the actual PCM gain directly measurable."""
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(44100)
        stream.writeframes(b'\x00\x20\x00\x20' * round(duration * 44100))
    return path


def fade_progress(curve, t):
    return {'linear': t, 'slow_fast': t * t, 'fast_slow': 1 - (1 - t) ** 2,
            's_curve': (1 - math.cos(math.pi * t)) / 2}[curve]


@pytest.mark.parametrize('curve', ['linear', 'slow_fast', 'fast_slow', 's_curve'])
@pytest.mark.parametrize('side', ['in', 'out'])
def test_each_curve_matches_actual_pcm_gain_in_time_order(tmp_path, side, curve):
    source = write_constant_level(tmp_path / 'reference.wav')
    before = digest(source)
    output = tmp_path / 'shaped.wav'
    editing.render_audio([clip(source, end=3, **{f'fade_{side}': 1, f'fade_{side}_curve': curve})], output)
    info, samples = read_wav(output)
    assert info.nframes == 3 * 44100
    assert samples[round(1.5 * 44100)] == pytest.approx(.25, abs=1 / 32768)
    start = 0 if side == 'in' else 2
    for t in (0, .1, .25, .5, .75, .9, 1):
        progress = fade_progress(curve, t)
        expected = progress if side == 'in' else 1 - progress
        frame = min(round((start + t) * 44100), len(samples) - 1)
        assert samples[frame] / .25 == pytest.approx(expected, abs=.0003)
    assert digest(source) == before


def test_fade_shapes_are_independent_and_default_is_identical_to_explicit_linear(tmp_path):
    source = write_constant_level(tmp_path / 'reference.wav')
    implicit, explicit, shaped = [tmp_path / name for name in ('implicit.wav', 'explicit.wav', 'shaped.wav')]
    base = clip(source, end=3, fade_in=1, fade_out=1)
    editing.render_audio([base], implicit)
    editing.render_audio([base | {'fade_in_curve': 'linear', 'fade_out_curve': 'linear'}], explicit)
    assert read_wav(implicit) == read_wav(explicit)
    editing.render_audio([base | {'fade_in_curve': 'slow_fast', 'fade_out_curve': 'fast_slow'}], shaped)
    _, samples = read_wav(shaped)
    assert samples[round(.25 * 44100)] / .25 == pytest.approx(.25 ** 2, abs=.0003)
    assert samples[round(2.25 * 44100)] / .25 == pytest.approx((1 - .25) ** 2, abs=.0003)
    assert samples[round(1.5 * 44100)] == pytest.approx(.25, abs=1 / 32768)


@pytest.mark.parametrize('field', ['fade_in_curve', 'fade_out_curve'])
@pytest.mark.parametrize('value', ['unknown', '', 'tri', None, [], {}, True, 1])
def test_invalid_curve_is_rejected_even_with_zero_duration_before_creating_output(tmp_path, field, value):
    source = write_tones(tmp_path / 'source.wav', [(440, .1)])
    before = digest(source)
    target = tmp_path / 'invalid.wav'
    with pytest.raises(ValueError, match='曲线'):
        editing.render_audio([clip(source, end=.1, **{field: value})], target)
    assert not target.exists() and digest(source) == before


def test_positive_gain_limits_peaks_without_claiming_normalization(tmp_path):
    source = write_tones(tmp_path / "loud.wav", [(440, .5)], channels=2, amplitude=.8)
    output = tmp_path / "protected.wav"
    result = editing.render_audio([clip(source, end=.5, gain_db=12)], output, master_gain_db=12)
    _, values = read_wav(output)
    assert max(abs(value) for value in values) <= .951
    assert rms(window(values, .1, .4)) > .4
    assert any("过载保护" in warning for warning in result["warnings"])


def test_mp3_output_decodes_to_exact_audio_and_preserves_source(tmp_path):
    source = write_tones(tmp_path / "input.wav", [(660, .5)])
    before = digest(source)
    output = tmp_path / "edited.mp3"
    result = editing.render_audio([clip(source, .1, .5)], output, output_format="mp3")
    raw = subprocess.run([
        shutil.which("ffmpeg"), "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
        "-i", str(output), "-ac", "1", "-ar", "44100", "-f", "f32le", "pipe:1",
    ], capture_output=True, check=True, timeout=20, shell=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0).stdout
    decoded = array.array("f", raw)
    if sys.byteorder != "little":
        decoded.byteswap()
    assert len(decoded) / 44100 == pytest.approx(.4, abs=1 / 44100)
    assert tone_strength(decoded, 660) > tone_strength(decoded, 440) * 20
    assert .4 <= result["duration"] < .46 and result["waveform"]
    assert digest(source) == before


@pytest.mark.parametrize("changes", [
    {"start": -.1}, {"end": 1.1}, {"start": .8, "end": .7}, {"end": .019},
    {"gain_db": 13}, {"gain_db": -61}, {"gain_db": float("nan")},
    {"start": float("inf")}, {"end": float("nan")},
    {"fade_in": 1.1}, {"fade_out": -.1}, {"fade_in": .6, "fade_out": .5},
])
def test_invalid_clip_settings_never_create_output_or_change_original(tmp_path, changes):
    source = write_tones(tmp_path / "input.wav", [(440, 1)])
    before = digest(source)
    target = tmp_path / "must not exist.wav"
    with pytest.raises(ValueError):
        editing.render_audio([clip(source, **changes)], target)
    assert not target.exists() and digest(source) == before


@pytest.mark.parametrize("options", [{"gap": -1}, {"gap": 6}, {"gap": float("nan")}, {"master_gain_db": 13}, {"master_gain_db": -25}, {"master_gain_db": float("inf")}, {"output_format": "ogg"}])
def test_invalid_global_options_are_rejected_before_output(tmp_path, options):
    source = write_tones(tmp_path / "input.wav", [(440, .1)])
    output = tmp_path / "output.wav"
    with pytest.raises(ValueError):
        editing.render_audio([clip(source, end=.1)], output, **options)
    assert not output.exists()


def test_existing_targets_and_input_path_are_never_overwritten(tmp_path):
    source = write_tones(tmp_path / "source.wav", [(440, .1)])
    original_hash = digest(source)
    with pytest.raises(ValueError, match="覆盖|原文件|新文件"):
        editing.render_audio([clip(source, end=.1)], source)
    target = tmp_path / "existing.wav"
    target.write_bytes(b"do not overwrite this user file")
    with pytest.raises(ValueError, match="覆盖|原文件|新文件"):
        editing.render_audio([clip(source, end=.1)], target)
    assert target.read_bytes() == b"do not overwrite this user file"
    assert digest(source) == original_hash


def test_clip_count_total_duration_and_gap_limits(tmp_path):
    source = write_tones(tmp_path / "long.wav", [(440, 21)], rate=8000)
    target = tmp_path / "limited.wav"
    for clips in ([], [clip(source, end=1)] * 13):
        with pytest.raises(ValueError, match="1–12"):
            editing.render_audio(clips, target)
    with pytest.raises(ValueError, match="240 秒"):
        editing.render_audio([clip(source, end=21)] * 12, target)
    with pytest.raises(ValueError, match="240 秒"):
        editing.render_audio([clip(source, end=20)] * 12, target, gap=.1)
    assert not target.exists()


def test_missing_ffmpeg_is_actionable_and_does_not_touch_source(tmp_path, monkeypatch):
    source = write_tones(tmp_path / "input.wav", [(440, .1)])
    before = digest(source)
    monkeypatch.setattr(editing.shutil, "which", lambda executable: None)
    target = tmp_path / "output.wav"
    with pytest.raises(ValueError, match="FFmpeg"):
        editing.render_audio([clip(source, end=.1)], target)
    assert not target.exists() and digest(source) == before


def test_corrupt_input_fails_without_deleting_input(tmp_path):
    source = tmp_path / "corrupt.wav"
    source.write_bytes(b"this is not audio")
    target = tmp_path / "output.wav"
    with pytest.raises(ValueError, match="失败|损坏|有效"):
        editing.render_audio([clip(source, end=.1)], target)
    assert not target.exists() and source.read_bytes() == b"this is not audio"


def test_render_timeout_cleans_only_new_partial_output(tmp_path, monkeypatch):
    source = write_tones(tmp_path / "source.wav", [(440, .2)])
    before = digest(source)
    target = tmp_path / "timed out.wav"
    run = subprocess.run

    def timeout_render(command, **kwargs):
        if "-filter_complex" in command:
            Path(command[-1]).write_bytes(b"partial new output")
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return run(command, **kwargs)

    monkeypatch.setattr(editing.subprocess, "run", timeout_render)
    with pytest.raises(ValueError, match="超时"):
        editing.render_audio([clip(source, end=.2)], target)
    assert not target.exists() and digest(source) == before


def test_output_over_size_limit_is_removed_without_changing_input(tmp_path, monkeypatch):
    source = write_tones(tmp_path / "source.wav", [(440, .1)])
    before = digest(source)
    target = tmp_path / "too large.mp3"
    monkeypatch.setattr(editing, "MAX_BYTES", 100)
    with pytest.raises(ValueError, match="50 MB"):
        editing.render_audio([clip(source, end=.1)], target, output_format="mp3")
    assert not target.exists() and digest(source) == before


def test_ffmpeg_invocations_use_local_protocols_no_shell_and_hidden_windows(tmp_path, monkeypatch):
    source = write_tones(tmp_path / "file with 中文 spaces.wav", [(440, .1)])
    output = tmp_path / "安全输出.wav"
    original_run = subprocess.run
    calls = []

    def observe(command, **kwargs):
        calls.append((command, kwargs))
        return original_run(command, **kwargs)

    monkeypatch.setattr(editing.subprocess, "run", observe)
    editing.render_audio([clip(source, end=.1)], output)
    assert calls
    for command, kwargs in calls:
        assert isinstance(command, list) and kwargs.get("shell", False) is False
        assert "-protocol_whitelist" in command
        assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"
        if os.name == "nt":
            assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
