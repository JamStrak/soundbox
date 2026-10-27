"""Quality workflows in isolated libraries: real DSP, provenance and explicit I/O."""
from __future__ import annotations

import hashlib
import io
import math
import struct
import threading
import time
import wave

import httpx
import pytest
from fastapi.testclient import TestClient

from server import gamepacks, openverse, providers, quality
from server.app import create_app
from server.audio import base_sound
from server.quality_api import FILE_BUDGET, BATCH_BUDGET

HEADERS = {'X-Soundbox-Client': 'local'}
REMOTE_ID = 'ov-12345678-1234-4234-8234-123456789abc'


def wav_data(amplitude=.025, seconds=.3):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(44100)
        stream.writeframes(b''.join(struct.pack('<h', round(amplitude * 32767 * math.sin(2 * math.pi * 440 * n / 44100)))
                                   for n in range(round(seconds * 44100))))
    return buffer.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('SOUNDBOX_NO_SEED', '1')
    app = create_app(tmp_path)
    for identifier, amplitude in [('quiet', .025), ('silent', 0)]:
        path = app.state.library.media / (identifier + '.wav')
        path.write_bytes(wav_data(amplitude))
        sound = base_sound(identifier, identifier, 'local') | {
            'duration': .3, 'license': 'CC BY', 'license_url': 'https://creativecommons.org/licenses/by/4.0/',
            'author': 'Test author', 'source_url': 'https://example.org/credits'}
        app.state.library.put(sound, path)
    with TestClient(app, headers=HEADERS) as api:
        yield api


def completed(client, task):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = client.get('/api/audio-tasks/' + task['id']).json()
        if result['status'] not in {'queued', 'running'}:
            return result
        time.sleep(.03)
    pytest.fail('Audio task failed to finish in 30 seconds')


def start_check(client, ids):
    response = client.post('/api/audio-checks', json={'ids': ids})
    assert response.status_code == 200, response.text
    return completed(client, response.json())


def remote_sound():
    return base_sound(REMOTE_ID, 'Remote click', 'openverse', 'mp3') | {
        'source_id': REMOTE_ID[3:], 'saved': False, 'downloaded': False,
        'quality': 'preview', 'remote_preview_url': 'https://cdn.freesound.org/previews/1/1-test-hq.mp3',
        'preview_url': 'https://cdn.freesound.org/previews/1/1-test-hq.mp3'}


def test_checks_distinguish_quiet_from_silent_and_survive_restart(client):
    assert client.get('/api/library').json()['total'] == 2
    before = {s: client.app.state.library.file(s).read_bytes() for s in ['quiet', 'silent']}
    task = start_check(client, ['quiet', 'silent'])
    assert task['status'] == 'complete' and not task['errors']
    checks = {s['id']: s['audio_check'] for s in task['items']}
    assert checks['quiet']['status'] == 'ready' and 'quiet' in checks['quiet']['flags']
    assert checks['silent']['status'] == 'unusable'
    assert client.get('/api/library').json()['items'][0]['id'] == 'quiet'
    assert client.get('/api/library?visibility=hidden').json()['items'][0]['id'] == 'silent'
    assert client.get('/api/library?visibility=all').json()['total'] == 2
    assert client.get('/api/library?audio_filter=quiet').json()['total'] == 1
    directory = client.app.state.library.directory
    with TestClient(create_app(directory), headers=HEADERS) as restarted:
        assert restarted.get('/api/library?audio_filter=checked').json()['total'] == 1
    for identifier, data in before.items():
        assert client.app.state.library.file(identifier).read_bytes() == data


def test_user_hide_restore_is_reversible_and_persistent_without_network(client, monkeypatch):
    monkeypatch.setattr(openverse, 'openverse_detail', lambda *_: pytest.fail('hiding needs no network'))
    assert client.patch('/api/sounds/quiet/visibility', json={'hidden': True}).status_code == 200
    assert client.get('/api/library').json()['total'] == 1
    assert client.get('/api/library?visibility=hidden').json()['items'][0]['id'] == 'quiet'
    assert client.patch('/api/sounds/' + REMOTE_ID + '/visibility', json={'hidden': True}).status_code == 200
    assert client.app.state.library.get(REMOTE_ID) is None
    with TestClient(create_app(client.app.state.library.directory), headers=HEADERS) as restarted:
        assert restarted.get('/api/library?visibility=hidden').json()['total'] == 1
        assert restarted.patch('/api/sounds/quiet/visibility', json={'hidden': False}).status_code == 200
        assert restarted.get('/api/library').json()['total'] == 2
    assert client.app.state.library.file('quiet').is_file()


def test_remote_checks_cache_without_adding_sounds_or_calling_ai(client, monkeypatch):
    calls = []
    sound = remote_sound()
    monkeypatch.setattr(openverse, 'openverse_detail', lambda *_: sound.copy())
    monkeypatch.setattr(providers, 'translate_query', lambda *_: pytest.fail('inspection cannot call text AI'))

    def download(item, **options):
        calls.append(options['max_bytes'])
        options['on_bytes'](len(wav_data()))
        return wav_data(), 'mp3'

    monkeypatch.setattr(openverse, 'download_openverse', download)
    task = start_check(client, [REMOTE_ID])
    assert task['status'] == 'complete' and not task['errors']
    assert task['items'][0]['audio_check']['status'] == 'ready'
    assert not task['items'][0]['saved'] and not task['items'][0]['downloaded']
    assert client.app.state.library.get(REMOTE_ID) is None
    start_check(client, [REMOTE_ID])
    assert calls == [FILE_BUDGET]


def test_compensation_is_real_repeatable_and_preserves_credits_and_original(client):
    path = client.app.state.library.file('quiet')
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    first = client.post('/api/sounds/quiet/compensate')
    assert first.status_code == 200, first.text
    preview = first.json()
    assert 0 < preview['gain_db'] <= 6
    assert preview['waveform'] and preview['duration'] > 0
    assert client.get(preview['preview_url'], headers={'Range': 'bytes=0-43'}).status_code == 206
    assert client.post('/api/sounds/quiet/compensate').json()['preview_url'] == preview['preview_url']
    saved = client.post('/api/sounds/quiet/compensate/save', json={'name': 'Test leveled'}).json()
    assert saved['source'] == 'edited' and saved['license'] == 'CC BY'
    assert saved['edit_sources'][0]['author'] == 'Test author'
    assert saved['compensation']['source_id'] == 'quiet'
    assert client.post('/api/sounds/quiet/compensate/save', json={'name': 'Test leveled'}).json()['id'] == saved['id']
    report = quality.analyze_audio(client.app.state.library.file(saved['id']))
    assert report['estimated_peak_dbfs'] <= -1.9
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash
    assert client.post('/api/sounds/silent/compensate').status_code == 400


def test_file_replacement_invalidates_report(client):
    start_check(client, ['quiet'])
    path = client.app.state.library.file('quiet')
    path.write_bytes(wav_data(.7, .4))
    assert client.get('/api/library?audio_filter=checked').json()['total'] == 0
    task = start_check(client, ['quiet'])
    assert 'quiet' not in task['items'][0]['audio_check']['flags']


def test_existing_lossy_preview_cannot_override_pcm_range_response(client):
    path = client.app.state.library.file('quiet')
    path.with_name(path.stem + '.listen.mp3').write_bytes(b'old-lossy-preview')
    full = client.get('/api/audio/quiet')
    assert full.content == path.read_bytes()
    assert full.headers['content-type'].startswith('audio/wav')
    partial = client.get('/api/audio/quiet', headers={'Range': 'bytes=20-43'})
    assert partial.status_code == 206 and partial.content == path.read_bytes()[20:44]


def test_check_cancel_stops_following_items(client, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    actual = quality.analyze_audio
    calls = []

    def slow(path):
        calls.append(path)
        entered.set()
        assert release.wait(5)
        return actual(path)

    monkeypatch.setattr(quality, 'analyze_audio', slow)
    task = client.post('/api/audio-checks', json={'ids': ['quiet', 'silent']}).json()
    try:
        assert entered.wait(5)
        assert client.post('/api/audio-tasks/' + task['id'] + '/cancel').status_code == 200
    finally:
        release.set()
    result = completed(client, task)
    assert result['status'] == 'cancelled' and len(calls) == 1


def test_failed_check_is_not_a_quality_rejection(client, monkeypatch):
    monkeypatch.setattr(quality, 'analyze_audio', lambda *_: (_ for _ in ()).throw(ValueError('检查超时，请重试。')))
    task = start_check(client, ['quiet'])
    assert task['errors'] and task['completed'] == 1
    sound = next(s for s in client.get('/api/library').json()['items'] if s['id'] == 'quiet')
    assert 'audio_check' not in sound and sound['audio_check_error']
    assert client.get('/api/library?audio_filter=unchecked').json()['total'] == 2


def test_budget_reader_stops_oversize_and_cancellation():
    response = httpx.Response(200, headers={'Content-Type': 'audio/wav'}, content=wav_data(), request=httpx.Request('GET', 'https://example.org'))
    with pytest.raises(providers.ProviderError, match='预算'):
        providers._read_audio(response, 'test', max_bytes=10)
    response = httpx.Response(200, headers={'Content-Type': 'audio/wav'}, content=wav_data(), request=httpx.Request('GET', 'https://example.org'))
    with pytest.raises(providers.ProviderError, match='取消'):
        providers._read_audio(response, 'test', cancelled=lambda: True)


def test_batch_budget_includes_failed_transfers(client, monkeypatch):
    calls = []
    monkeypatch.setattr(openverse, 'openverse_detail', lambda ident: remote_sound() | {'id': 'ov-' + ident, 'source_id': ident})

    def download(sound, **options):
        calls.append(sound['id'])
        options['on_bytes'](FILE_BUDGET)
        raise providers.ProviderError('测试大音频无法检查。', 413)

    monkeypatch.setattr(openverse, 'download_openverse', download)
    ids = ['ov-12345678-1234-4234-8234-' + f'{n:012d}' for n in range(10)]
    result = start_check(client, ids)
    assert len(calls) == BATCH_BUDGET // FILE_BUDGET
    assert result['downloaded_bytes'] == BATCH_BUDGET and len(result['errors']) == 10


def test_game_pack_routes_and_variant_filtering(client, monkeypatch):
    monkeypatch.setattr(gamepacks, 'list_packs', lambda _: [{'id': 'test', 'count': 2, 'bytes': 200, 'installed_count': 0}])

    def install(identifier, library, progress=None, cancelled=None):
        sounds = []
        for n in range(2):
            sound = base_sound('kenney-test-' + str(n), 'metal impact', 'kenney') | {
                'variant_group': 'test-metal', 'tags': ['metal', 'impact'], 'pack_id': 'test'}
            library.put(sound, library.file('quiet'))
            sounds.append(sound)
            progress(n + 1, 2, '导入中')
        return sounds

    monkeypatch.setattr(gamepacks, 'install_pack', install)
    task = completed(client, client.post('/api/game-packs/test/install').json())
    assert task['status'] == 'complete' and task['completed'] == 2
    result = client.get('/api/library', params={'pack_only': True, 'collapse_variants': True, 'q': '金属撞击'}).json()
    assert len(result['items']) == 1 and result['total'] == 2
    assert result['collapsed_count'] == 1 and result['items'][0]['variant_count'] == 2
    assert client.get('/api/library?pack_only=true&collapse_variants=false').json()['total'] == 2


def test_quality_mutations_require_local_header_and_validate_ids(client):
    assert client.post('/api/audio-checks', json={'ids': ['quiet']}, headers={'X-Soundbox-Client': ''}).status_code == 403
    assert client.post('/api/audio-checks', json={'ids': ['quiet'] * 11}).status_code == 422
    assert client.post('/api/audio-checks', json={'ids': ['../private']}).status_code == 400
    assert client.post('/api/game-packs/unknown/install').status_code == 400
    assert client.post('/api/sounds/quiet/compensate/save', json={'name': '  '}).status_code == 400


def test_failed_compensation_save_cleans_only_new_file(client, monkeypatch):
    from server import quality_api
    assert client.post('/api/sounds/quiet/compensate').status_code == 200
    before = set(client.app.state.library.media.iterdir())

    def fail(source, target):
        target.write_bytes(b'partial')
        raise OSError('disk full')

    monkeypatch.setattr(quality_api.shutil, 'copyfile', fail)
    assert client.post('/api/sounds/quiet/compensate/save', json={}).status_code == 400
    assert set(client.app.state.library.media.iterdir()) == before


def test_committed_compensation_file_survives_response_failure(client, monkeypatch):
    library = client.app.state.library
    actual = library.put

    def fail_after_commit(sound, path=None):
        actual(sound, path)
        raise OSError('response failure')

    monkeypatch.setattr(library, 'put', fail_after_commit)
    assert client.post('/api/sounds/quiet/compensate/save', json={}).status_code == 400
    monkeypatch.setattr(library, 'put', actual)
    saved = client.post('/api/sounds/quiet/compensate/save', json={}).json()
    assert library.file(saved['id']).is_file()
    assert len([s for s in library.all() if s['source'] == 'edited']) == 1


def test_disappearing_check_cache_does_not_break_library_listing(client, monkeypatch):
    from server import quality_api
    start_check(client, ['quiet'])
    monkeypatch.setattr(quality_api, 'stamp', lambda _: (_ for _ in ()).throw(FileNotFoundError()))
    response = client.get('/api/library')
    assert response.status_code == 200
    assert all('audio_check' not in s for s in response.json()['items'])
