from __future__ import annotations

import io
import time
import wave
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import providers
from server.app import create_app
from server.audio import base_sound, inspect_audio, synthesize
from server.settings import Settings

HEADERS = {'X-Soundbox-Client': 'local'}


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(44100)
        f.writeframes(synthesize('ui-click', .12).tobytes())
    return output.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('SOUNDBOX_NO_SEED', '1')
    application = create_app(tmp_path)
    lib = application.state.library
    path = lib.media / 'sword.wav'
    path.write_bytes(wav_bytes())
    lib.put(base_sound('sword', 'Sword Whoosh', 'local') | {
        'tags': ['sword', 'whoosh', 'combat'], 'duration': .12, 'license': 'CC0',
    }, path)
    lib.put(base_sound('rain', '雨声', 'local') | {'tags': ['环境', '雨'], 'duration': 8, 'license': '待确认'}, path)
    with TestClient(application, headers=HEADERS) as c:
        yield c


def test_bilingual_library_and_filters(client):
    assert client.get('/api/library', params={'q': '挥剑'}).json()['items'][0]['id'] == 'sword'
    assert client.get('/api/library', params={'q': 'sword'}).json()['total'] == 1
    assert client.get('/api/library', params={'q': 'rain'}).json()['items'][0]['id'] == 'rain'
    assert client.get('/api/library', params={'license': 'cc0'}).json()['total'] == 1
    assert client.get('/api/library', params={'duration_max': 1}).json()['total'] == 1


def test_favorite_folder_and_notes_survive_restart(client, monkeypatch):
    folder = client.post('/api/folders', json={'name': '战斗'}).json()
    child = client.post('/api/folders', json={'name': '刀剑', 'parent_id': folder['id']}).json()
    changed = client.patch('/api/sounds/sword', json={'favorite': True, 'folder_id': child['id'],
                                                   'tags': ['挥剑', 'sword'], 'note': '关卡一'}).json()
    assert changed['favorite']
    directory = client.app.state.library.directory
    with TestClient(create_app(directory), headers=HEADERS) as restarted:
        items = restarted.get('/api/library', params={'favorite': True, 'folder_id': child['id']}).json()['items']
        assert len(items) == 1 and items[0]['note'] == '关卡一'
        assert len(restarted.get('/api/folders').json()['items']) == 2


def test_folder_cycle_rejected_and_delete_preserves_sounds(client):
    parent = client.post('/api/folders', json={'name': '项目'}).json()
    child = client.post('/api/folders', json={'name': '子目录', 'parent_id': parent['id']}).json()
    assert client.patch('/api/folders/' + parent['id'], json={'parent_id': child['id']}).status_code == 400
    assert client.post('/api/folders', json={'name': '项目'}).status_code == 400
    assert client.post('/api/folders', json={'name': '   '}).status_code == 422
    client.patch('/api/sounds/sword', json={'folder_id': child['id']})
    assert client.delete('/api/folders/' + parent['id']).status_code == 200
    assert client.get('/api/folders').json()['items'] == []
    assert client.get('/api/library').json()['total'] == 2
    assert client.get('/api/audio/sword').status_code == 200
    assert client.get('/api/library', params={'folder_id': 'unfiled'}).json()['total'] == 2


def test_partial_sound_updates_do_not_wipe_existing_fields(client):
    client.patch('/api/sounds/sword', json={'note': '重要素材', 'tags': ['金属']})
    sound = client.patch('/api/sounds/sword', json={'favorite': True}).json()
    assert sound['note'] == '重要素材' and sound['tags'] == ['金属']
    assert client.patch('/api/sounds/sword', json={'folder_id': 'missing'}).status_code == 400


def test_credentials_protected_and_omitted_values_retained(client):
    secret = 'not-a-real-provider-key-SFX'
    result = client.patch('/api/settings', json={'elevenlabs_key': secret, 'elevenlabs_license': 'personal'})
    assert result.json()['elevenlabs_key_set'] and secret not in result.text
    assert secret not in client.get('/api/settings').text
    client.patch('/api/settings', json={'text_model': 'custom-model'})
    vault = Settings(client.app.state.library.directory)
    assert vault.get()['elevenlabs_key'] == secret
    assert secret.encode() not in vault.path.read_bytes()
    assert client.patch('/api/settings', json={'elevenlabs_key': ''}).json()['elevenlabs_key_set'] is False


def test_change_freesound_account_invalidates_tokens(client):
    client.app.state.settings.update({'freesound_key': 'old', 'freesound_access_token': 'access',
                                      'freesound_refresh_token': 'refresh'})
    client.patch('/api/settings', json={'freesound_key': 'new'})
    assert client.app.state.settings.get()['freesound_refresh_token'] == ''


def test_untrusted_origin_and_host_cannot_modify_library(client):
    result = client.post('/api/folders', json={'name': 'evil'}, headers={'Origin': 'https://evil.example'})
    assert result.status_code == 403
    result = client.post('/api/folders', json={'name': 'evil'}, headers={'X-Soundbox-Client': ''})
    assert result.status_code == 403
    assert client.get('/api/settings', headers={'Host': 'evil.example'}).status_code == 403
    assert client.get('/api/folders').json()['items'] == []


def test_audio_range_and_export_with_attribution(client):
    result = client.get('/api/audio/sword', headers={'Range': 'bytes=0-43'})
    assert result.status_code == 206 and result.content[:4] == b'RIFF'
    assert len(result.content) == 44
    client.patch('/api/sounds/sword', json={'name': '../CON', 'note': '=danger()'})
    exported = client.post('/api/export', json={'ids': ['sword', 'rain']})
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert all('/' not in n and '\\' not in n for n in archive.namelist())
        assert len(archive.namelist()) == 3
        attribution = archive.read('attribution.csv').decode('utf-8-sig')
        assert "'=danger()" in attribution and 'CC0' in attribution


def test_export_missing_audio_never_silently_omits(client):
    client.app.state.library.put(base_sound('missing-file', '尚未下载', 'freesound'))
    result = client.post('/api/export', json={'ids': ['sword', 'missing-file']})
    assert result.status_code == 400 and '尚未下载' in result.json()['detail']


def test_import_real_audio_and_reject_invalid_file(client):
    result = client.post('/api/import', files=[('files', ('中文 click.wav', wav_bytes(), 'audio/wav')),
                                              ('files', ('broken.wav', b'not sound', 'audio/wav')),
                                              ('files', ('danger.html', b'<script>', 'text/html'))])
    assert result.status_code == 200
    assert len(result.json()['items']) == 1 and len(result.json()['errors']) == 2
    sound = result.json()['items'][0]
    assert sound['duration'] > 0 and len(sound['waveform']) > 0
    assert sound['license'] == '待确认'
    assert client.get('/api/audio/' + sound['id']).status_code == 200
    assert client.get('/api/sounds/' + sound['id'] + '/file').content[:4] == b'RIFF'


def test_remove_import_does_not_delete_original_audio(client):
    original = client.app.state.library.file('sword')
    assert client.delete('/api/sounds/sword').status_code == 200
    assert original.exists()
    assert client.get('/api/audio/sword').status_code == 404


def test_missing_keys_errors_are_actionable(client):
    result = client.get('/api/search?q=sword&provider=freesound')
    assert result.status_code == 400 and 'Openverse' in result.json()['detail']
    result = client.post('/api/generate', json={'prompt': '雨声', 'duration': 3, 'loop': True})
    assert result.status_code == 400 and 'ElevenLabs' in result.json()['detail']


def test_remote_save_retrieves_trusted_metadata(client, monkeypatch):
    calls = []

    def detail(sound_id, configuration):
        calls.append(sound_id)
        return base_sound('fs-42', 'Trusted sound', 'freesound') | {'source_id': '42', 'preview_url': 'https://cdn.freesound.org/previews/safe.mp3'}

    monkeypatch.setattr(providers, 'freesound_detail', detail)
    result = client.post('/api/sounds/save', json={'sound': {'id': 'fs-42', 'source': 'freesound',
                                                             'preview_url': 'http://127.0.0.1/secrets'}, 'favorite': True})
    assert result.status_code == 200
    assert calls == ['42'] and result.json()['name'] == 'Trusted sound'
    assert 'secrets' not in result.text
    assert client.post('/api/sounds/save', json={'sound': {'id': '../../secret', 'source': 'freesound'}}).status_code == 400


def wait_job(client, job_id):
    for _ in range(100):
        result = client.get('/api/jobs/' + job_id).json()
        if result['status'] in {'complete', 'failed'}:
            return result
        time.sleep(.05)
    pytest.fail('background generation did not finish')


def test_generation_persists_audio_and_license_without_real_provider(client, monkeypatch):
    client.app.state.settings.update({'elevenlabs_key': 'test-key', 'elevenlabs_license': 'personal'})
    calls = []

    def generate(prompt, duration, loop, settings):
        calls.append((prompt, duration, loop))
        return wav_bytes(), 'wav'

    monkeypatch.setattr(providers, 'generate_elevenlabs', generate)
    response = client.post('/api/generate', json={'prompt': '一声短促的 UI 点击', 'duration': .5, 'loop': False})
    assert response.status_code == 200
    result = wait_job(client, response.json()['id'])
    assert result['status'] == 'complete'
    assert calls == [('一声短促的 UI 点击', .5, False)]
    sound = result['sound']
    assert sound['license'] == 'Personal' and sound['source'] == 'elevenlabs'
    assert client.get('/api/sounds/' + sound['id'] + '/file').content[:4] == b'RIFF'
    assert all(s['id'] != sound['id'] for s in client.get('/api/library?license=commercial').json()['items'])


def test_generation_failure_not_retried_or_reported_success(client, monkeypatch):
    client.app.state.settings.update({'elevenlabs_key': 'test-key'})
    calls = []

    def fail(*args):
        calls.append(1)
        raise providers.ProviderError('额度不足', 429)

    monkeypatch.setattr(providers, 'generate_elevenlabs', fail)
    response = client.post('/api/generate', json={'prompt': 'rain', 'duration': 3, 'loop': False})
    result = wait_job(client, response.json()['id'])
    assert result['status'] == 'failed' and '额度不足' in result['error']
    assert len(calls) == 1 and client.get('/api/stats').json()['generated'] == 0


def test_invalid_parameters_and_secret_routes(client):
    assert client.post('/api/generate', json={'prompt': 'sword', 'duration': 60, 'loop': False}).status_code == 422
    assert client.patch('/api/settings', json={'text_api_base': 'http://example.com'}).status_code == 400
    assert client.patch('/api/settings', json={'text_api_base': 'https://user:pass@example.com/v1'}).status_code == 400
    assert client.get('/data/settings.vault').status_code == 404
    assert client.get('/api/does-not-exist').status_code == 404
