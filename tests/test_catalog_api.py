"""Search/listen/save/editor integration in isolated data; no live provider calls."""
from copy import deepcopy
import hashlib
import io
import time
import wave
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from server import app as app_module, catalog

HEADERS = {'X-Soundbox-Client': 'local'}


def wav_data():
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(44100)
        writer.writeframes(b'\x01\x10\x01\xf0' * 4410)
    return buffer.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    audio = wav_data()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('Audio/metal_hit.wav', audio)
        archive.writestr('License.txt', 'CC0 Example author')
    data = buffer.getvalue()
    manifest = {'version': 1, 'packs': [{
        'id': 'example', 'name': '测试金属素材包', 'description': '材质声音',
        'author': 'Example author', 'original_name': 'Example Sounds', 'license': 'CC0',
        'license_url': 'https://creativecommons.org/publicdomain/zero/1.0/',
        'source_url': 'https://opengameart.org/content/example',
        'license_evidence_url': 'https://opengameart.org/content/example',
        'download_url': 'https://opengameart.org/sites/default/files/example.zip',
        'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
        'files': [{'path': 'Audio/metal_hit.wav', 'name': '金属撞击 · Metal Hit',
                   'tags': ['metal', 'impact'], 'duration': .2, 'format': 'wav',
                   'bytes': len(audio), 'sha256': hashlib.sha256(audio).hexdigest(),
                   'sample_rate': 44100, 'channels': 1, 'codec': 'pcm_s16le'}]
    }]}
    calls = []

    def handler(request):
        calls.append(str(request.url))
        assert str(request.url) == manifest['packs'][0]['download_url']
        assert 'authorization' not in request.headers
        return httpx.Response(200, content=data)

    monkeypatch.setattr(catalog.providers, '_client', lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(app_module, 'Catalog', lambda directory: catalog.Catalog(directory, manifest))
    monkeypatch.setenv('SOUNDBOX_NO_SEED', '1')
    application = app_module.create_app(tmp_path / 'data')
    with TestClient(application, headers=HEADERS) as connection:
        yield connection, calls, audio, tmp_path


def finish_task(connection, initial):
    task = initial
    end = time.monotonic() + 20
    while task['status'] in {'queued', 'running'} and time.monotonic() < end:
        time.sleep(.02)
        task = connection.get('/api/audio-tasks/' + task['id']).json()
    assert task['status'] not in {'queued', 'running'}, task
    return task


def test_search_and_pack_listing_do_not_download_or_call_ai(client):
    connection, calls, _, _ = client
    for query in ('金属撞击', 'metal hit', ''):
        result = connection.get('/api/search', params={'provider': 'catalog', 'q': query, 'pack_id': 'example'})
        assert result.status_code == 200, result.text
        assert result.json()['total'] == 1
        assert not result.json()['items'][0]['saved']
    assert connection.get('/api/catalog/packs').json()['items'][0]['count'] == 1
    assert connection.get('/api/library').json()['total'] == 0
    assert not calls
    assert connection.get('/api/search?provider=catalog&pack_id=forged').status_code == 400


def test_listen_range_save_and_daw_export_use_canonical_source(client):
    connection, calls, original, _ = client
    sound = connection.get('/api/search?provider=catalog&q=metal').json()['items'][0]
    sound_id = sound['id']
    check = connection.post('/api/audio-checks', json={'ids': [sound_id]})
    task = finish_task(connection, check.json())
    assert any('先试听' in error for error in task['errors']) and not calls
    ready = connection.post('/api/catalog/previews/' + sound_id)
    assert ready.status_code == 200, ready.text
    assert ready.json()['catalog_cached'] and len(ready.json()['waveform']) == 96
    assert connection.get('/api/library').json()['total'] == 0
    ranged = connection.get('/api/catalog/audio/' + sound_id, headers={'Range': 'bytes=0-43'})
    assert ranged.status_code == 206 and ranged.content == original[:44]
    assert len(calls) == 1
    checked = finish_task(connection, connection.post('/api/audio-checks', json={'ids': [sound_id]}).json())
    assert checked['items'][0].get('audio_check')
    compensate_path = '/api/sounds/' + sound_id + '/compensate'
    first_compensation = connection.post(compensate_path)
    assert first_compensation.status_code == 200, first_compensation.text
    connection.post('/api/catalog/previews/' + sound_id)
    second_compensation = connection.post(compensate_path)
    assert second_compensation.status_code == 200
    assert first_compensation.json()['preview_url'] == second_compensation.json()['preview_url']
    forged = deepcopy(sound) | {'license': 'Fake', 'name': 'Fake', 'download_url': 'http://127.0.0.1/private'}
    saved = connection.post('/api/sounds/save', json={'sound': forged, 'favorite': True, 'download': True})
    assert saved.status_code == 200, saved.text
    saved = saved.json()
    assert saved['name'] == sound['name'] and saved['license'] == 'CC0'
    assert saved['quality'] == 'original' and saved['downloaded'] and saved['favorite']
    assert connection.get('/api/sounds/' + sound_id + '/file').content == original
    assert connection.get('/api/library?pack_only=true').json()['total'] == 1
    after = connection.post('/api/catalog/previews/' + sound_id).json()
    assert after['saved'] and after['favorite']
    rendered = connection.post('/api/editor/render', json={'clips': [
        {'sound_id': sound_id, 'start': 0, 'end': .12, 'gain_db': 0, 'fade_in': .01, 'fade_out': .01}],
        'master_gain_db': 0, 'gap': 0, 'format': 'wav'})
    assert rendered.status_code == 200, rendered.text
    draft = rendered.json()
    exported = connection.get('/api/editor/previews/' + draft['id'] + '/download?name=DAW%20clip')
    assert exported.status_code == 200 and exported.content[:4] == b'RIFF'
    assert 'DAW' in exported.headers['content-disposition']
    assert len(calls) == 1


def test_local_pack_index_is_lazy_and_reindex_preserves_personal_fields(client):
    connection, calls, original, tmp_path = client
    directory = tmp_path / 'downloads'
    directory.mkdir()
    archive_path = directory / 'Personal SFX.zip'
    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('My Sounds/Metal impact.wav', original)
        archive.writestr('Readme.txt', 'Test private pack; no open license')
    response = connection.post('/api/local-packs/index', json={'path': str(directory)})
    assert response.status_code == 200, response.text
    task = finish_task(connection, response.json())
    assert task['status'] == 'complete', task
    sound = connection.get('/api/library?pack_only=true').json()['items'][0]
    assert sound['source'] == 'localpack' and not sound['downloaded'] and sound['saved']
    assert connection.get('/api/local-packs').json()['total'] == 1
    data_dir = connection.app.state.library.directory
    assert not list((data_dir / 'local-pack-cache').glob('*.wav')) and not calls
    edited = connection.patch('/api/sounds/' + sound['id'], json={'name': '我的碰撞', 'favorite': True, 'note': 'Keep'})
    assert edited.status_code == 200
    reindex = connection.post('/api/local-packs/index', json={'path': str(directory)})
    assert finish_task(connection, reindex.json())['status'] == 'complete'
    current = connection.get('/api/library').json()['items'][0]
    assert current['name'] == '我的碰撞' and current['favorite'] and current['note'] == 'Keep'
    preview = connection.post('/api/local-packs/previews/' + sound['id'])
    assert preview.status_code == 200, preview.text
    assert preview.json()['name'] == '我的碰撞' and not preview.json()['downloaded']
    downloaded = connection.post('/api/sounds/' + sound['id'] + '/download', json={'quality': 'original'})
    assert downloaded.status_code == 200, downloaded.text
    assert connection.get('/api/sounds/' + sound['id'] + '/file').content == original
    assert archive_path.is_file() and not calls


def test_missing_zip_invalidates_check_without_breaking_library(client):
    connection, _, original, tmp_path = client
    archive_path = tmp_path / 'Private.zip'
    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('Metal.wav', original)
    response = connection.post('/api/local-packs/index', json={'path': str(archive_path)})
    assert finish_task(connection, response.json())['status'] == 'complete'
    sound = connection.get('/api/library').json()['items'][0]
    assert connection.post('/api/local-packs/previews/' + sound['id']).status_code == 200
    check = connection.post('/api/audio-checks', json={'ids': [sound['id']]})
    assert finish_task(connection, check.json())['items'][0].get('audio_check')
    archive_path.unlink()
    result = connection.get('/api/library')
    assert result.status_code == 200, result.text
    assert result.json()['total'] == 1
    assert not result.json()['items'][0].get('audio_check')
    assert connection.post('/api/local-packs/previews/' + sound['id']).status_code == 400


def test_local_index_requires_same_origin_header_and_rejects_unknown_preview(client):
    connection, _, _, _ = client
    response = connection.post('/api/local-packs/index', json={'path': 'missing'}, headers={'X-Soundbox-Client': ''})
    assert response.status_code == 403
    assert connection.post('/api/catalog/previews/forged').status_code == 400
    assert connection.post('/api/local-packs/previews/forged').status_code == 400
