"""Actual codec/container validation, independent of filenames and legacy caches."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

os.environ.setdefault('SOUNDBOX_DATA_DIR', str(Path(tempfile.gettempdir()) / 'soundbox-playback-tests'))

from server import audio


@pytest.fixture(autouse=True)
def ffmpeg_available():
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('Real browser playback checks require FFmpeg and ffprobe')


def generate(path, codec, container, channels=1):
    subprocess.run([shutil.which('ffmpeg'), '-nostdin', '-v', 'error', '-y', '-f', 'lavfi',
                    '-i', 'sine=frequency=1000:duration=0.1', '-ac', str(channels),
                    '-c:a', codec, '-f', container, str(path)], check=True, capture_output=True,
                   timeout=20, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    return path


@pytest.mark.parametrize('codec,container,suffix,mime', [
    ('libmp3lame', 'mp3', '.mp3', 'audio/mpeg'),
    ('libvorbis', 'ogg', '.ogg', 'audio/ogg'),
    ('pcm_s16le', 'wav', '.wav', 'audio/wav'),
])
def test_verified_formats_bypass_legacy_128k_cache(tmp_path, codec, container, suffix, mime):
    source = generate(tmp_path / ('original' + suffix), codec, container, channels=2)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    legacy = source.with_name(source.stem + '.listen.mp3')
    legacy.write_bytes(b'old preview must remain untouched')
    audio.make_browser_preview(source)
    assert audio.browser_audio_path(source) == source
    assert audio.browser_audio_mime(source) == mime
    assert legacy.read_bytes() == b'old preview must remain untouched'
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_actual_mp3_with_wrong_suffix_uses_correct_mime(tmp_path):
    source = generate(tmp_path / 'misnamed.wav', 'libmp3lame', 'mp3')
    assert audio.browser_audio_path(source) == source
    assert audio.browser_audio_mime(source) == 'audio/mpeg'


@pytest.mark.parametrize('codec,container,channels', [('flac', 'flac', 1), ('pcm_s24le', 'wav', 1), ('pcm_s16le', 'wav', 6)])
def test_unsupported_actual_encoding_creates_compatible_mp3(tmp_path, codec, container, channels):
    source = generate(tmp_path / 'fake.mp3', codec, container, channels)
    audio.make_browser_preview(source)
    chosen = audio.browser_audio_path(source)
    assert chosen != source and chosen.is_file()
    assert audio.browser_audio_mime(chosen) == 'audio/mpeg'


def test_probe_cache_reuses_unchanged_file_and_invalidates_replacement(tmp_path, monkeypatch):
    source = generate(tmp_path / 'switch.mp3', 'libmp3lame', 'mp3')
    real_run = audio.run_audio
    calls = []
    def track(command, **kwargs):
        calls.append(command)
        return real_run(command, **kwargs)
    monkeypatch.setattr(audio, 'run_audio', track)
    assert audio.browser_audio_mime(source) == 'audio/mpeg'
    assert audio.browser_audio_path(source) == source
    assert len(calls) == 1
    generate(source, 'pcm_s16le', 'wav')
    assert audio.browser_audio_mime(source) == 'audio/wav'
    assert len(calls) == 2
