"""Local FFmpeg regressions; these tests never import the default app or user DB."""
import array
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import wave

import pytest

os.environ.setdefault('SOUNDBOX_DATA_DIR', str(Path(tempfile.gettempdir()) / 'soundbox-quality-tests'))

from server import quality


@pytest.fixture(autouse=True)
def ffmpeg_available():
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('Real quality regressions require FFmpeg and ffprobe')


def write_wav(path, values, channels=1, rate=44100):
    packed = array.array('h', values)
    if sys.byteorder != 'little':
        packed.byteswap()
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(packed.tobytes())
    return path


def tone(seconds=.4, amplitude=.01, rate=44100):
    return [round(32767 * amplitude * math.sin(2 * math.pi * 1000 * i / rate))
            for i in range(round(seconds * rate))]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_quiet_stereo_analysis_and_linear_copy_preserve_channels_and_source(tmp_path):
    # Opposite polarity channels must not cancel through a mono downmix.
    original = write_wav(tmp_path / 'quiet.wav', [v for x in tone() for v in (x, -x)], channels=2)
    before = digest(original)
    report = quality.analyze_audio(original)
    assert report['version'] == 1 and report['status'] == 'ready'
    assert 'quiet' in report['flags'] and 'silent' not in report['flags']
    assert report['peak_dbfs'] == pytest.approx(-40, abs=.1)
    assert report['rms_dbfs'] == pytest.approx(-43.01, abs=.1)
    assert report['gain_db'] == pytest.approx(6)
    json.dumps(report, allow_nan=False)
    target = tmp_path / 'compensated.wav'
    result = quality.render_compensated(original, target, report)
    assert result['applied_gain_db'] == pytest.approx(6)
    assert result['rms_dbfs'] - report['rms_dbfs'] == pytest.approx(6, abs=.03)
    assert result['duration'] == pytest.approx(report['duration'], abs=1 / 44100)
    with wave.open(str(target)) as stream:
        assert (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) == (2, 2, 44100)
    assert digest(original) == before


def test_digital_zero_only_is_silent_with_json_null_levels(tmp_path):
    source = write_wav(tmp_path / 'zero.wav', [0] * 4410)
    result = quality.analyze_audio(source)
    assert result['status'] == 'unusable' and result['flags'] == ['silent']
    assert all(result[key] is None for key in ('peak_dbfs', 'rms_dbfs', 'estimated_peak_dbfs', 'gain_db'))
    json.dumps(result, allow_nan=False)
    target = tmp_path / 'rejected.wav'
    with pytest.raises(ValueError):
        quality.render_compensated(source, target)
    assert not target.exists()


def test_smallest_nonzero_pcm_remains_usable(tmp_path):
    source = write_wav(tmp_path / 'faint.wav', [1, -1] * 2205)
    result = quality.analyze_audio(source)
    assert result['status'] == 'ready' and 'quiet' in result['flags']
    assert 'silent' not in result['flags']


def test_short_strong_transient_is_not_amplified_by_low_rms(tmp_path):
    source = write_wav(tmp_path / 'impulse.wav', [0] * 220 + [30000] + [0] * 1984)
    report = quality.analyze_audio(source)
    assert report['status'] == 'ready' and 'strong_transient' in report['flags']
    assert report['rms_dbfs'] < -30 and report['gain_db'] <= 0
    target = tmp_path / 'safe.wav'
    result = quality.render_compensated(source, target, report)
    assert result['estimated_peak_dbfs'] <= -2 + .02
    assert result['duration'] == pytest.approx(.05, abs=1 / 44100)


def test_long_edges_are_marked_but_not_trimmed(tmp_path):
    source = write_wav(tmp_path / 'tail.wav', [0] * 13230 + tone(.2, .05) + [0] * 22050)
    report = quality.analyze_audio(source)
    assert report['status'] == 'ready' and 'long_edges' in report['flags']
    assert report['leading_seconds'] == pytest.approx(.3, abs=.001)
    assert report['trailing_seconds'] == pytest.approx(.5, abs=.001)
    result = quality.render_compensated(source, tmp_path / 'tail-copy.wav', report)
    assert result['duration'] == pytest.approx(1, abs=.001)
    assert result['leading_seconds'] == pytest.approx(.3, abs=.001)
    assert result['trailing_seconds'] == pytest.approx(.5, abs=.001)


def test_designed_square_wave_is_only_peak_warning(tmp_path):
    source = write_wav(tmp_path / 'square.wav', [30000 if i % 40 < 20 else -30000 for i in range(4410)])
    report = quality.analyze_audio(source)
    assert report['status'] == 'ready' and 'peak_risk' in report['flags']
    assert 'silent' not in report['flags'] and 'decode_error' not in report['flags']
    result = quality.render_compensated(source, tmp_path / 'square-copy.wav', report)
    assert result['estimated_peak_dbfs'] <= -2 + .02


def test_corrupt_audio_is_unusable_without_private_path_in_report(tmp_path):
    source = tmp_path / 'private-title.mp3'
    source.write_bytes(b'not an audio file')
    result = quality.analyze_audio(source)
    assert result['status'] == 'unusable' and result['flags'] == ['decode_error']
    assert 'private-title' not in json.dumps(result, ensure_ascii=False)
    json.dumps(result, allow_nan=False)


def test_existing_targets_and_source_are_never_replaced(tmp_path):
    source = write_wav(tmp_path / 'source.wav', tone())
    before = digest(source)
    for target in [source, tmp_path / 'exists.wav']:
        if target != source:
            target.write_bytes(b'keep this')
        expected = target.read_bytes()
        with pytest.raises(ValueError):
            quality.render_compensated(source, target)
        assert target.read_bytes() == expected
    assert digest(source) == before


def test_environment_or_budget_failure_is_not_unusable(tmp_path, monkeypatch):
    source = write_wav(tmp_path / 'source.wav', tone())
    with monkeypatch.context() as patch:
        patch.setattr(quality.shutil, 'which', lambda name: None)
        with pytest.raises(ValueError):
            quality.analyze_audio(source)
    with monkeypatch.context() as patch:
        patch.setattr(quality, 'MAX_SECONDS', .01)
        with pytest.raises(ValueError):
            quality.analyze_audio(source)


def test_render_timeout_removes_only_its_reserved_target(tmp_path, monkeypatch):
    source = write_wav(tmp_path / 'source.wav', tone())
    report = quality.analyze_audio(source)
    before = digest(source)
    neighbor = tmp_path / 'neighbor.wav'
    neighbor.write_bytes(b'preserve')
    monkeypatch.setattr(quality, 'RENDER_TIMEOUT', .0001)
    target = tmp_path / 'timed-out.wav'
    with pytest.raises(ValueError):
        quality.render_compensated(source, target, report)
    assert not target.exists() and digest(source) == before and neighbor.read_bytes() == b'preserve'


def test_untrusted_or_stale_analysis_cannot_bypass_peak_limit(tmp_path):
    source = write_wav(tmp_path / 'source.wav', tone(amplitude=.95))
    forged = {'version': 1, 'status': 'ready', 'gain_db': 1000, 'estimated_peak_dbfs': -80}
    result = quality.render_compensated(source, tmp_path / 'bounded.wav', forged)
    assert result['applied_gain_db'] <= 0
    assert result['estimated_peak_dbfs'] <= -2 + .02
