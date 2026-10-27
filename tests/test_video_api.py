"""Local video drafts, real extraction/editing, and bounded temporary storage.

Never use the personal library: isolate SOUNDBOX_DATA_DIR before collection.
"""
import array
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import threading
import uuid
import wave

import pytest
from fastapi.testclient import TestClient

assert os.environ.get('SOUNDBOX_DATA_DIR'), 'Set isolated SOUNDBOX_DATA_DIR before collecting tests'
from server.app import create_app
from server import editing


HEADERS = {'X-Soundbox-Client': 'local'}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('SOUNDBOX_NO_SEED', '1')
    with TestClient(create_app(tmp_path / 'library'), headers=HEADERS) as connection:
        yield connection


@pytest.fixture(scope='session')
def videos(tmp_path_factory):
    """Tiny real containers exercise the media boundary without network fixtures."""
    executable = shutil.which('ffmpeg')
    if not executable or not shutil.which('ffprobe'):
        pytest.skip('Video extraction requires FFmpeg and FFprobe')
    root = tmp_path_factory.mktemp('video-fixtures')
    cache = {}

    def make(kind='mp4'):
        if kind in cache:
            return cache[kind]
        suffix = {'silent': 'mp4', 'audio_only': 'm4a', 'multiple_audio': 'mp4'}.get(kind, kind)
        target = root / (kind + '.' + suffix)
        args = [executable, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y']
        if kind != 'audio_only':
            args += ['-f', 'lavfi', '-i', 'color=c=navy:s=64x48:r=10:d=1.5']
        if kind != 'silent':
            args += ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100:duration=1.5']
        if kind == 'multiple_audio':
            args += ['-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=44100:duration=1.5',
                     '-map', '0:v:0', '-map', '1:a:0', '-map', '2:a:0']
        if kind == 'audio_only':
            args += ['-c:a', 'aac']
        elif kind == 'webm':
            args += ['-c:v', 'libvpx', '-deadline', 'realtime', '-c:a', 'libopus']
        elif kind in {'mov', 'avi', 'mkv'}:
            args += ['-c:v', 'mpeg4', '-c:a', 'pcm_s16le']
        else:
            args += ['-c:v', 'mpeg4', '-c:a', 'aac']
        if kind == 'm4v':
            args += ['-f', 'mp4']
        args += ['-threads', '1', str(target)]
        completed = subprocess.run(args, capture_output=True, timeout=20,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        assert completed.returncode == 0, completed.stderr.decode('utf-8', errors='replace')
        cache[kind] = target
        return target

    return make


def upload(client, path, *, draft_id=None, filename=None):
    draft_id = draft_id or uuid.uuid4().hex
    response = client.post('/api/video-sources/' + draft_id,
                           files={'file': (filename or path.name, path.read_bytes(), 'video/mp4')})
    return draft_id, response


def render(client, sound_id):
    return client.post('/api/editor/render', json={
        'clips': [{'sound_id': sound_id, 'start': .25, 'end': 1.1,
                   'gain_db': -3, 'fade_in': .1, 'fade_out': .15}],
        'master_gain_db': 0, 'gap': 0, 'format': 'wav',
    })


def draft_files(client):
    root = client.app.state.library.directory / 'video-sources'
    return [path for path in root.rglob('*') if path.is_file()]


def test_extract_edit_save_folder_and_provenance_preserve_original(client, videos):
    source = videos()
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    draft_id, response = upload(client, source, filename=r'C:\fakepath\AI火焰视频.MP4')
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft['id'] == 'video-' + draft_id and draft['source'] == 'video'
    assert not draft['saved'] and draft['downloaded'] and draft['format'] == 'wav'
    assert draft['duration'] == pytest.approx(1.5, abs=.1)
    assert draft['waveform'] and draft['license'] != 'CC0'
    assert client.get('/api/stats').json()['total'] == 0
    assert not list(client.app.state.library.media.iterdir()), 'Extraction must remain a draft'
    audio = client.get(draft['preview_url'], headers={'Range': 'bytes=0-43'})
    assert audio.status_code == 206 and audio.content[:4] == b'RIFF'
    preview_response = render(client, draft['id'])
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview['duration'] == pytest.approx(.85, abs=.02)
    with wave.open(io.BytesIO(client.get(preview['preview_url']).content), 'rb') as stream:
        edited_samples = array.array('h', stream.readframes(stream.getnframes()))
        channels = stream.getnchannels()
        rate = stream.getframerate()
    window = rate // 100 * channels
    def rms(samples):
        return math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    center = edited_samples[len(edited_samples) // 2:len(edited_samples) // 2 + window]
    assert rms(edited_samples[:window]) < rms(center) * .2
    assert rms(edited_samples[-window:]) < rms(center) * .2
    folder = client.post('/api/folders', json={'name': '视频采集音效'}).json()
    saved_response = client.post('/api/editor/save', json={
        'preview_id': preview['id'], 'name': '火焰起手', 'folder_id': folder['id'],
    })
    assert saved_response.status_code == 200, saved_response.text
    saved = saved_response.json()
    assert saved['folder_id'] == folder['id'] and saved['source'] == 'edited'
    assert saved['downloaded'] and saved['license'] != 'CC0'
    assert saved['edit_recipe']['clips'][0]['start'] == .25
    assert saved['edit_sources'][0]['source'] == 'video'
    assert 'AI火焰视频.MP4' in json.dumps(saved['edit_sources'], ensure_ascii=False)
    assert 'fakepath' not in json.dumps(saved, ensure_ascii=False)
    assert 'video' in saved['origin_source']
    assert client.get('/api/stats').json()['total'] == 1
    assert client.delete('/api/video-sources/' + draft_id).status_code == 200
    assert client.get(draft['preview_url']).status_code == 404
    assert not draft_files(client)
    assert client.get('/api/sounds/' + saved['id'] + '/file').content[:4] == b'RIFF'
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    # Final output and source attribution survive a later app instance.
    with TestClient(create_app(client.app.state.library.directory), headers=HEADERS) as restarted:
        stored = restarted.get('/api/library?source=edited').json()['items']
        assert len(stored) == 1 and stored[0]['id'] == saved['id']
        assert restarted.get('/api/audio/' + saved['id']).status_code == 200


@pytest.mark.parametrize('kind', ['mov', 'm4v', 'webm', 'mkv', 'avi'])
def test_supported_containers_decode_into_browser_compatible_wav(client, videos, kind):
    draft_id, response = upload(client, videos(kind))
    assert response.status_code == 200, response.text
    audio = client.get(response.json()['preview_url'])
    with wave.open(io.BytesIO(audio.content), 'rb') as stream:
        assert stream.getnchannels() == 2 and stream.getsampwidth() == 2
        assert stream.getframerate() == 44100 and stream.getnframes() > 40000
    assert client.delete('/api/video-sources/' + draft_id).status_code == 200


def test_multiple_audio_tracks_extract_first_track(client, videos):
    _, response = upload(client, videos('multiple_audio'))
    assert response.status_code == 200, response.text
    with wave.open(io.BytesIO(client.get(response.json()['preview_url']).content), 'rb') as stream:
        samples = array.array('h', stream.readframes(stream.getnframes()))[::stream.getnchannels()]
        rate = stream.getframerate()
    samples = samples[rate // 5:rate]
    def magnitude(frequency):
        real = sum(sample * math.cos(2 * math.pi * frequency * i / rate) for i, sample in enumerate(samples))
        imag = sum(sample * math.sin(2 * math.pi * frequency * i / rate) for i, sample in enumerate(samples))
        return math.hypot(real, imag)
    assert magnitude(440) > magnitude(880) * 10


@pytest.mark.parametrize('kind', ['silent', 'audio_only'])
def test_requires_real_video_and_audio_streams(client, videos, kind):
    _, response = upload(client, videos(kind), filename='looks-like-video.mp4')
    assert response.status_code == 400, response.text
    assert not draft_files(client) and client.get('/api/stats').json()['total'] == 0


@pytest.mark.parametrize('content', [b'not a video', b'', b'\x00\x00\x00\x18ftypmp42truncated'])
def test_corrupt_video_cleans_all_temporary_files(client, content):
    response = client.post('/api/video-sources/' + uuid.uuid4().hex,
                           files={'file': ('broken.mp4', content, 'video/mp4')})
    assert response.status_code == 400, response.text
    assert not draft_files(client)


def test_disguised_playlist_cannot_read_local_video(client, videos):
    private_source = videos().resolve().as_posix()
    content = f"ffconcat version 1.0\nfile '{private_source}'\n"
    response = client.post('/api/video-sources/' + uuid.uuid4().hex,
                           files={'file': ('fake.mp4', content.encode(), 'video/mp4')})
    assert response.status_code == 400, response.text
    assert not draft_files(client) and client.get('/api/stats').json()['total'] == 0


def test_cancel_before_upload_prevents_late_draft_creation(client, videos):
    draft_id = uuid.uuid4().hex
    url = '/api/video-sources/' + draft_id
    assert client.delete(url).status_code == 200
    assert client.delete(url).status_code == 200
    _, response = upload(client, videos(), draft_id=draft_id)
    assert response.status_code == 409, response.text
    assert client.get(url + '/audio').status_code == 404 and not draft_files(client)


def test_duplicate_id_cannot_overwrite_existing_draft(client, videos):
    draft_id, first = upload(client, videos())
    assert first.status_code == 200, first.text
    original = client.get(first.json()['preview_url']).content
    _, duplicate = upload(client, videos(), draft_id=draft_id)
    assert duplicate.status_code == 409, duplicate.text
    assert client.get(first.json()['preview_url']).content == original


def test_duration_and_file_size_limits_leave_no_drafts(client, videos, monkeypatch):
    from server import video_api
    source = videos()
    with monkeypatch.context() as limits:
        limits.setattr(video_api, 'MAX_VIDEO_SECONDS', .5)
        _, response = upload(client, source)
        assert response.status_code == 400, response.text
        assert not draft_files(client)
    with monkeypatch.context() as limits:
        limits.setattr(video_api, 'MAX_VIDEO_BYTES', 64)
        _, response = upload(client, source)
        assert response.status_code == 413, response.text
        assert not draft_files(client)
    _, retry = upload(client, source)
    assert retry.status_code == 200, retry.text


def test_actual_stream_limit_applies_without_content_length(client, videos, monkeypatch):
    from server import video_api
    monkeypatch.setattr(video_api, 'MAX_VIDEO_BYTES', 64)
    monkeypatch.setattr(video_api, 'MULTIPART_OVERHEAD', 256)
    boundary = 'soundbox-video-fixture-boundary'
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="video.mp4"\r\n'
            'Content-Type: video/mp4\r\n\r\n').encode() + videos().read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
    # An iterator makes HTTPX use Transfer-Encoding: chunked rather than a
    # convenient Content-Length that middleware could trust instead of counting.
    chunks = (body[offset:offset + 128] for offset in range(0, len(body), 128))
    response = client.post('/api/video-sources/' + uuid.uuid4().hex, content=chunks,
                           headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
    assert response.status_code == 413, response.text
    assert not draft_files(client)


def test_cancel_during_extraction_stops_late_result_and_frees_slot(client, videos, monkeypatch):
    from server import video_api
    entered, release = threading.Event(), threading.Event()
    actual = video_api.extract_audio
    source = videos()
    draft_id = uuid.uuid4().hex

    def gated(*args, **kwargs):
        entered.set()
        assert release.wait(8)
        return actual(*args, **kwargs)

    monkeypatch.setattr(video_api, 'extract_audio', gated)
    with ThreadPoolExecutor(max_workers=2) as pool:
        importing = pool.submit(upload, client, source, draft_id=draft_id)
        try:
            assert entered.wait(5)
            _, parallel = upload(client, source)
            assert parallel.status_code == 409, parallel.text
            assert client.delete('/api/video-sources/' + draft_id).status_code == 200
        finally:
            release.set()
        _, cancelled = importing.result(timeout=15)
    assert cancelled.status_code == 409, cancelled.text
    assert not draft_files(client) and client.get('/api/stats').json()['total'] == 0
    monkeypatch.setattr(video_api, 'extract_audio', actual)
    _, retry = upload(client, source)
    assert retry.status_code == 200, retry.text


def test_capacity_preserves_other_windows_draft_until_explicitly_closed(client, videos, monkeypatch):
    from server import video_api
    monkeypatch.setattr(video_api, 'MAX_DRAFTS', 1)
    draft_id, first = upload(client, videos())
    assert first.status_code == 200, first.text
    _, full = upload(client, videos())
    assert full.status_code == 409, full.text
    assert client.get(first.json()['preview_url']).status_code == 200
    assert client.delete('/api/video-sources/' + draft_id).status_code == 200
    _, retry = upload(client, videos())
    assert retry.status_code == 200, retry.text


def test_native_video_audio_request_is_recognized_by_hub(client, videos):
    lifecycle = client.app.state.hub_lifecycle
    lifecycle.session({'sessionId': 'video_page_01', 'action': 'heartbeat'})
    client.headers['X-Hub-Session'] = 'video_page_01'
    _, response = upload(client, videos())
    assert response.status_code == 200, response.text
    assert not lifecycle.untracked
    del client.headers['X-Hub-Session']
    # An <audio> element cannot attach the browser bridge's fetch headers.
    result = client.get(response.json()['preview_url'], headers={'Range': 'bytes=0-43'})
    assert result.status_code == 206 and not lifecycle.untracked


def test_expired_draft_is_unavailable_and_cannot_be_rendered(client, videos, monkeypatch):
    from server import video_api
    _, response = upload(client, videos())
    assert response.status_code == 200, response.text
    draft = response.json()
    monkeypatch.setattr(video_api, 'DRAFT_TTL_SECONDS', -1)
    assert client.get(draft['preview_url']).status_code == 404
    assert render(client, draft['id']).status_code in {400, 404}
    assert not draft_files(client)


def test_cancel_during_render_preserves_leased_source_until_render_finishes(client, videos, monkeypatch):
    draft_id, response = upload(client, videos())
    assert response.status_code == 200, response.text
    entered, release = threading.Event(), threading.Event()
    actual = editing.render_audio

    def gated(*args, **kwargs):
        entered.set()
        assert release.wait(8)
        return actual(*args, **kwargs)

    monkeypatch.setattr(editing, 'render_audio', gated)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rendering = pool.submit(render, client, response.json()['id'])
        try:
            assert entered.wait(5)
            assert client.delete('/api/video-sources/' + draft_id).status_code == 200
            assert client.get(response.json()['preview_url']).status_code == 404
            assert draft_files(client), 'The active render still holds a source lease'
        finally:
            release.set()
        preview = rendering.result(timeout=15)
    assert preview.status_code == 200, preview.text
    assert not draft_files(client)
    saved = client.post('/api/editor/save', json={'preview_id': preview.json()['id'], 'name': '已完成试听'})
    assert saved.status_code == 200, saved.text


def test_import_auth_and_id_validation_are_not_bypassed(client, videos):
    data = videos().read_bytes()
    response = client.post('/api/video-sources/' + uuid.uuid4().hex,
                           files={'file': ('video.mp4', data, 'video/mp4')},
                           headers={'X-Soundbox-Client': 'untrusted'})
    assert response.status_code == 403
    invalid = client.post('/api/video-sources/not-a-draft-id', files={'file': ('video.mp4', data)})
    assert invalid.status_code in {400, 404, 422}, invalid.text
    assert not draft_files(client)
