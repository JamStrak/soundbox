"""Non-destructive editor API and saved provenance, with real audio files."""
import array
import hashlib
import io
import math
import os
import threading
import wave
import zipfile
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest

from server.app import create_app
from server.audio import base_sound, inspect_audio
from server import editing
from server.editor_api import provenance


HEADERS = {'X-Soundbox-Client': 'local'}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('SOUNDBOX_NO_SEED', '1')
    application = create_app(tmp_path)
    library = application.state.library
    for index, frequency in enumerate((440, 880)):
        sound_id = f'tone-{index}'
        path = library.media / (sound_id + '.wav')
        with wave.open(str(path), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(22050)
            stream.writeframes(array.array('h', (int(5000 * math.sin(2 * math.pi * frequency * i / 22050)) for i in range(44100))).tobytes())
        library.put(base_sound(sound_id, f'Tone {index}', 'local') | inspect_audio(path) | {
            'license': 'CC BY 4.0' if index == 0 else 'CC0',
            'license_url': 'https://creativecommons.org/licenses/by/4.0/' if index == 0 else 'https://creativecommons.org/publicdomain/zero/1.0/',
            'author': f'Author {index}', 'source_url': f'https://example.org/audio/{index}',
            'attribution': f'Tone {index} by Author {index}', 'tags': ['test'], 'original_filename': path.name,
        }, path)
    with TestClient(application, headers=HEADERS) as connection:
        yield connection


def recipe(**changes):
    return {'clips': [{'sound_id': 'tone-0', 'start': .25, 'end': 1.25, 'gain_db': -6,
                       'fade_in': .1, 'fade_out': .2}], 'master_gain_db': 0, 'gap': 0, 'format': 'wav', **changes}


@pytest.mark.parametrize('curves', [{}, {'fade_in_curve': 'slow_fast', 'fade_out_curve': 'fast_slow'},
                                    {'fade_in_curve': 's_curve', 'fade_out_curve': 'linear'}])
def test_real_preview_save_download_preserve_source_and_license(client, curves):
    library = client.app.state.library
    original = library.file('tone-0')
    before = hashlib.sha256(original.read_bytes()).hexdigest()
    edit_recipe = recipe()
    edit_recipe['clips'][0].update(curves)
    response = client.post('/api/editor/render', json=edit_recipe)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview['duration'] == pytest.approx(1, abs=.02)
    assert preview['waveform'] and preview['format'] == 'wav'
    assert client.get('/api/stats').json()['total'] == 2, 'preview must not add library items'
    audio = client.get(preview['preview_url'], headers={'Range': 'bytes=0-43'})
    assert audio.status_code == 206 and audio.content[:4] == b'RIFF'
    folder = client.post('/api/folders', json={'name': '剪辑成品'}).json()
    payload = {'preview_id': preview['id'], 'name': '剑击片段', 'folder_id': folder['id']}
    saved_response = client.post('/api/editor/save', json=payload)
    assert saved_response.status_code == 200, saved_response.text
    saved = saved_response.json()
    assert saved['source'] == 'edited' and saved['quality'] == 'edited' and saved['downloaded']
    assert saved['folder_id'] == folder['id'] and saved['license'] == 'CC BY 4.0'
    assert saved['edit_sources'][0]['author'] == 'Author 0'
    assert saved['edit_sources'][0]['original_filename'] == 'tone-0.wav'
    assert saved['edit_recipe']['clips'][0]['start'] == .25
    for field in ('fade_in_curve', 'fade_out_curve'):
        assert saved['edit_recipe']['clips'][0][field] == curves.get(field, 'linear')
    assert 'Author 0' in saved['attribution'] and 'example.org/audio/0' in saved['attribution']
    again = client.post('/api/editor/save', json=payload)
    assert again.json()['id'] == saved['id'] and client.get('/api/stats').json()['total'] == 3
    assert client.delete(preview['preview_url']).status_code == 200
    assert client.get(preview['preview_url']).status_code == 404
    assert client.get('/api/sounds/' + saved['id'] + '/file').content[:4] == b'RIFF'
    assert hashlib.sha256(original.read_bytes()).hexdigest() == before
    with TestClient(create_app(library.directory), headers=HEADERS) as restarted:
        result = restarted.get('/api/library?source=edited').json()['items']
        assert len(result) == 1 and result[0]['folder_id'] == folder['id']
        assert result[0]['edit_recipe'] == saved['edit_recipe']
        assert result[0]['edit_sources'] == saved['edit_sources']
        assert restarted.get('/api/audio/' + saved['id']).status_code == 200
    exported = client.post('/api/export', json={'ids': [saved['id']]})
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        attribution = archive.read('attribution.csv').decode('utf-8-sig')
        assert 'Author 0' in attribution and 'CC BY 4.0' in attribution and 'example.org/audio/0' in attribution


def test_mixed_license_concat_cannot_be_relicensed_cc0(client):
    payload = recipe(clips=[{'sound_id': f'tone-{i}', 'start': 0, 'end': .3} for i in range(2)], gap=.1, format='mp3')
    preview = client.post('/api/editor/render', json=payload).json()
    assert preview['duration'] == pytest.approx(.7, abs=.08)
    saved = client.post('/api/editor/save', json={'preview_id': preview['id'], 'name': '拼接音效'}).json()
    assert saved['license'] == '混合许可 · 需核对' and saved['license_url'] == ''
    assert len(saved['edit_sources']) == 2
    assert 'Author 0' in saved['attribution'] and 'Author 1' in saved['attribution']
    assert all(item['id'] != saved['id'] for item in client.get('/api/library?license=cc0').json()['items'])
    assert len(client.get('/api/sounds/' + saved['id'] + '/file').content) > 1000


@pytest.mark.parametrize('clips', [[], [{'sound_id': '../private', 'start': 0, 'end': 1}],
    [{'sound_id': 'tone-0', 'start': 1, 'end': .5}], [{'sound_id': 'tone-0', 'start': 0, 'end': 30}],
    [{'sound_id': 'tone-0', 'start': 0, 'end': 1, 'path': 'C:/private.wav'}]])
def test_invalid_ranges_or_client_paths_never_create_outputs(client, clips):
    response = client.post('/api/editor/render', json=recipe(clips=clips))
    assert response.status_code in {400, 422}
    assert client.get('/api/stats').json()['total'] == 2
    assert not list((client.app.state.library.directory / 'editor-previews').iterdir())


@pytest.mark.parametrize('field', ['fade_in_curve', 'fade_out_curve'])
@pytest.mark.parametrize('value', ['unknown', '', 'tri', None, [], {}, True, 1])
def test_invalid_curve_parameters_never_render_or_create_files(client, monkeypatch, field, value):
    library = client.app.state.library
    original = library.file('tone-0')
    before = hashlib.sha256(original.read_bytes()).hexdigest()

    def must_not_render(*args, **kwargs):
        pytest.fail('invalid curve must be rejected before audio rendering')

    monkeypatch.setattr(editing, 'render_audio', must_not_render)
    payload = recipe()
    payload['clips'][0].update({field: value, 'fade_in': 0, 'fade_out': 0})
    response = client.post('/api/editor/render', json=payload)
    assert response.status_code == 422
    assert client.get('/api/stats').json()['total'] == 2
    assert not list((library.directory / 'editor-previews').iterdir())
    assert hashlib.sha256(original.read_bytes()).hexdigest() == before


def test_failed_render_cleanup_and_retry(client, monkeypatch):
    actual = editing.render_audio

    def fail(clips, target, **kwargs):
        target.write_bytes(b'partial')
        raise ValueError('音频处理失败')

    monkeypatch.setattr(editing, 'render_audio', fail)
    assert client.post('/api/editor/render', json=recipe()).status_code == 400
    assert not list((client.app.state.library.directory / 'editor-previews').iterdir())
    monkeypatch.setattr(editing, 'render_audio', actual)
    assert client.post('/api/editor/render', json=recipe()).status_code == 200


def test_parallel_render_rejected_without_blocking_library(client, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    actual = editing.render_audio

    def gated(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return actual(*args, **kwargs)

    monkeypatch.setattr(editing, 'render_audio', gated)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, '/api/editor/render', json=recipe())
        try:
            assert entered.wait(5)
            assert client.post('/api/editor/render', json=recipe()).status_code == 409
            assert client.get('/api/stats').status_code == 200
        finally:
            release.set()
        assert first.result(timeout=10).status_code == 200


def test_expired_or_missing_draft_does_not_save(client):
    assert client.post('/api/editor/save', json={'preview_id': 'f' * 32, 'name': '不存在'}).status_code == 404
    assert client.get('/api/editor/previews/../../data/settings.vault').status_code == 404
    assert client.delete('/api/editor/previews/' + 'f' * 32).status_code == 200


def test_reediting_openverse_keeps_original_provider_in_provenance():
    source = {'id': 'ov-id', 'source': 'openverse', 'origin_source': 'freesound', 'name': 'Metal',
              'license': 'CC0', 'author': 'Artist', 'source_url': 'https://freesound.org/sound/1'}
    once = provenance([source])
    twice = provenance([{'id': 'edit-id', 'source': 'edited', **once}])
    assert twice['origin_source'] == 'freesound'
    assert twice['edit_sources'][0]['source'] == 'openverse'
    assert twice['edit_sources'][0]['origin_source'] == 'freesound'
    assert twice['source_url'] == source['source_url']


@pytest.mark.skipif(os.name != 'nt', reason='Windows open-handle delete semantics')
def test_busy_preview_file_does_not_break_new_render_and_is_cleaned_later(client, monkeypatch):
    source = client.app.state.library.file('tone-0')

    def fast_render(clips, target, **kwargs):
        target.write_bytes(source.read_bytes())
        return {'duration': 2, 'waveform': [.5] * 96, 'warnings': []}

    monkeypatch.setattr(editing, 'render_audio', fast_render)
    generated = [client.post('/api/editor/render', json=recipe()).json() for _ in range(8)]
    registry = client.app.state.editor_previews
    first = generated[0]['id']
    path = registry[first]['path']
    with path.open('rb'):
        assert client.post('/api/editor/render', json=recipe()).status_code == 200
        assert first in registry and path.exists()
        assert client.delete('/api/editor/previews/' + first).status_code == 200
        assert first in registry and registry[first]['discarded']
    assert client.post('/api/editor/render', json=recipe()).status_code == 200
    assert first not in registry and not path.exists()


def test_response_failure_after_save_does_not_delete_committed_audio(client, monkeypatch):
    preview = client.post('/api/editor/render', json=recipe()).json()
    library = client.app.state.library
    actual = library.file
    fail_once = True

    def file_with_response_failure(sound_id):
        nonlocal fail_once
        if sound_id.startswith('edit-') and fail_once:
            fail_once = False
            raise OSError('simulated response failure after commit')
        return actual(sound_id)

    monkeypatch.setattr(library, 'file', file_with_response_failure)
    payload = {'preview_id': preview['id'], 'name': '需要重试响应'}
    with pytest.raises(OSError):
        client.post('/api/editor/save', json=payload)
    saved = client.post('/api/editor/save', json=payload).json()
    assert saved['downloaded'] and library.file(saved['id']).exists()
    assert client.get('/api/stats').json()['total'] == 3
