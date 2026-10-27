"""No-key search and saved public audio flow, without live network requests."""
import csv
import io
import wave
import zipfile

from fastapi.testclient import TestClient
import pytest

from server import openverse
from server.app import create_app
from server.audio import base_sound, synthesize


IDENTIFIER = '39918cd2-10ee-45a1-affd-eac4b2ec571f'
SOUND_ID = 'ov-' + IDENTIFIER
HEADERS = {'X-Soundbox-Client': 'local'}


def remote_sound():
    return base_sound(SOUND_ID, 'Metal Impact', 'openverse', '.mp3') | {
        'source_id': IDENTIFIER, 'origin_source': 'freesound', 'author': 'Rickplayer',
        'attribution': 'Metal Impact by Rickplayer, CC0 1.0', 'license': 'CC0 1.0',
        'license_url': 'https://creativecommons.org/publicdomain/zero/1.0/',
        'source_url': 'https://freesound.org/people/Rickplayer/sounds/530486/',
        'preview_url': 'https://cdn.freesound.org/previews/530/530486_2532248-hq.mp3',
        'remote_preview_url': 'https://cdn.freesound.org/previews/530/530486_2532248-hq.mp3',
        'download_label': '公开 MP3', 'quality': 'preview', 'saved': False,
        'downloaded': False, 'duration': 1.79,
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('SOUNDBOX_NO_SEED', '1')
    with TestClient(create_app(tmp_path), headers=HEADERS) as connection:
        yield connection


def test_default_online_search_works_without_any_key_and_expands_chinese(client, monkeypatch):
    seen = []

    def search(query, page, license_filter, duration_max, sort):
        seen.append((query['translated'], page, license_filter, duration_max, sort))
        return {'items': [remote_sound()], 'total': 240, 'page': page,
                'has_more': True, 'query': query}

    monkeypatch.setattr(openverse, 'search_openverse', search)
    settings = client.get('/api/settings').json()
    assert not settings['freesound_key_set'] and not settings['text_api_key_set']
    for keyword in ('金属撞击', 'metal impact'):
        response = client.get('/api/search', params={'q': keyword, 'license': 'cc0'})
        assert response.status_code == 200
        assert response.json()['items'][0]['source'] == 'openverse'
    assert seen == [('metal impact', 1, 'cc0', 0, 'score')] * 2
    assert client.get('/api/search?q=sword&provider=unknown').status_code == 422
    assert client.get('/api/search?q=sword&provider=freesound').status_code == 400


def test_save_download_export_and_reload_preserve_real_audio_and_attribution(client, monkeypatch):
    metadata_calls, downloads = [], []

    def detail(source_id):
        metadata_calls.append(source_id)
        return remote_sound()

    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(44100)
        stream.writeframes(synthesize('ui-click', .12).tobytes())

    def download(sound):
        downloads.append(sound['remote_preview_url'])
        return buffer.getvalue(), 'wav'

    monkeypatch.setattr(openverse, 'openverse_detail', detail)
    monkeypatch.setattr(openverse, 'download_openverse', download)
    folder = client.post('/api/folders', json={'name': '战斗音效'}).json()
    response = client.post('/api/sounds/save', json={
        'sound': {'id': SOUND_ID, 'source': 'openverse', 'preview_url': 'http://127.0.0.1/private',
                  'license': 'evil', 'name': 'evil'},
        'favorite': True, 'folder_id': folder['id'], 'download': True,
    })
    assert response.status_code == 200, response.text
    sound = response.json()
    assert sound['name'] == 'Metal Impact' and sound['license'] == 'CC0 1.0'
    assert sound['favorite'] and sound['downloaded'] and sound['folder_id'] == folder['id']
    assert sound['quality'] == 'preview' and len(sound['waveform']) > 0
    assert metadata_calls == [IDENTIFIER]
    assert downloads == [remote_sound()['remote_preview_url']]
    assert client.post('/api/sounds/' + SOUND_ID + '/download', json={'quality': 'preview'}).status_code == 200
    assert len(downloads) == 1
    assert client.get('/api/audio/' + SOUND_ID, headers={'Range': 'bytes=0-43'}).status_code == 206
    assert client.get('/api/sounds/' + SOUND_ID + '/file').content == buffer.getvalue()
    exported = client.post('/api/export', json={'ids': [SOUND_ID]})
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        rows = list(csv.DictReader(io.StringIO(archive.read('attribution.csv').decode('utf-8-sig'))))
        assert rows[0]['原始来源'] == 'freesound'
        assert rows[0]['署名说明'] == remote_sound()['attribution']
    with TestClient(create_app(client.app.state.library.directory), headers=HEADERS) as restarted:
        persisted = restarted.get('/api/library', params={'favorite': True, 'source': 'openverse'}).json()['items']
        assert len(persisted) == 1 and persisted[0]['downloaded']
        assert persisted[0]['folder_id'] == folder['id']


@pytest.mark.parametrize('sound_id, source_id', [('../../private', IDENTIFIER), (SOUND_ID, '../private'),
    (SOUND_ID, '49918cd2-10ee-45a1-affd-eac4b2ec571f')])
def test_save_rejects_invalid_or_mismatched_remote_identifier(client, monkeypatch, sound_id, source_id):
    monkeypatch.setattr(openverse, 'openverse_detail', lambda *_: pytest.fail('must not call provider'))
    response = client.post('/api/sounds/save', json={'sound': {
        'id': sound_id, 'source_id': source_id, 'source': 'openverse'}})
    assert response.status_code == 400


def test_openverse_original_never_silently_returns_a_preview(client, monkeypatch):
    client.app.state.library.put(remote_sound() | {'saved': True})
    monkeypatch.setattr(openverse, 'download_openverse', lambda *_: pytest.fail('must not download'))
    response = client.post('/api/sounds/' + SOUND_ID + '/download', json={'quality': 'original'})
    assert response.status_code == 400 and '来源页面' in response.json()['detail']


def test_search_retains_saved_user_metadata_and_local_preview(client, monkeypatch):
    client.app.state.library.put(remote_sound() | {'saved': True, 'favorite': True, 'note': '游戏内盾牌'})
    monkeypatch.setattr(openverse, 'search_openverse', lambda query, *_: {
        'items': [remote_sound()], 'total': 1, 'page': 1, 'has_more': False, 'query': query})
    sound = client.get('/api/search?q=metal').json()['items'][0]
    assert sound['favorite'] and sound['saved'] and sound['note'] == '游戏内盾牌'


def test_local_commercial_filter_recognizes_openverse_licenses(client):
    for index, license_name in enumerate(('CC0 1.0', 'CC BY 4.0', 'CC BY-SA 4.0', 'CC BY-NC 4.0', 'CC BY-ND 4.0', 'unknown')):
        client.app.state.library.put(base_sound(str(index), license_name, 'openverse') | {'license': license_name})
    assert client.get('/api/library?license=commercial').json()['total'] == 3
    assert client.get('/api/library?license=cc0').json()['total'] == 1
