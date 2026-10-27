"""Expired checks, stale errors and cancellation across the processing lock."""
import os
from pathlib import Path
import tempfile
import threading
import time
import wave

import pytest
from fastapi.testclient import TestClient

# Importing server.app constructs a default app: isolate before that import.
os.environ.setdefault('SOUNDBOX_DATA_DIR', str(Path(tempfile.gettempdir()) / 'soundbox-quality-state-tests'))
os.environ.setdefault('SOUNDBOX_NO_SEED', '1')

from server import quality
from server.app import create_app
from server.audio import base_sound
from server.quality_api import CHECK_VERSION, stamp


HEADERS = {'X-Soundbox-Client': 'local'}
READY = {'version': CHECK_VERSION, 'status': 'ready', 'flags': [],
         'peak_dbfs': -10.0, 'rms_dbfs': -20.0, 'estimated_peak_dbfs': -10.0,
         'leading_seconds': 0.0, 'trailing_seconds': 0.0, 'duration': .1,
         'gain_db': 0.0, 'message': 'Test measurement'}


@pytest.fixture
def api(tmp_path):
    app = create_app(tmp_path)
    path = app.state.library.media / 'sample.wav'
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(44100)
        output.writeframes(b'\x01\x00' * 4410)
    app.state.library.put(base_sound('sample', 'Sample', 'local'), path)
    with TestClient(app, headers=HEADERS) as client:
        yield client


def wait_terminal(api, identifier):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = api.get('/api/audio-tasks/' + identifier)
        assert response.status_code == 200
        task = response.json()
        if task['status'] not in {'queued', 'running'}:
            return task
        time.sleep(.01)
    pytest.fail('Cancelled task did not stop after the processing lock was released')


@pytest.mark.parametrize('invalidated_by', ['missing_file', 'replacement', 'version', 'missing_stamp'])
@pytest.mark.parametrize('old_status', ['ready', 'unusable'])
def test_inline_measurement_requires_current_file_stamp_and_version(api, invalidated_by, old_status):
    library = api.app.state.library
    path = library.file('sample')
    report = READY | {'status': old_status, 'flags': ['silent'] if old_status == 'unusable' else []}
    if invalidated_by == 'version':
        report['version'] = CHECK_VERSION + 1
    sound = library.get('sample') | {'audio_check': report, 'audio_check_stamp': stamp(path)}
    if invalidated_by == 'missing_stamp':
        sound.pop('audio_check_stamp')
    library.put(sound)
    if invalidated_by == 'missing_file':
        path.unlink()
    elif invalidated_by == 'replacement':
        path.write_bytes(path.read_bytes() + b'changed')

    # A stale unusable report must no longer hide the item by default either.
    result = api.get('/api/library').json()
    assert result['total'] == 1
    assert 'audio_check' not in result['items'][0]
    assert api.get('/api/library?audio_filter=checked').json()['total'] == 0
    assert api.get('/api/library?audio_filter=unchecked').json()['total'] == 1


def test_successful_recheck_clears_persisted_import_error_and_issue_filter(api, monkeypatch):
    library = api.app.state.library
    library.update('sample', {'audio_check_error': 'Previous import-time inspection timed out'})
    assert api.get('/api/library?audio_filter=issues').json()['total'] == 1
    monkeypatch.setattr(quality, 'analyze_audio', lambda _: READY.copy())
    sound, _, _ = api.app.state.audio_service.inspect('sample')
    assert sound['audio_check']['status'] == 'ready'
    assert 'audio_check_error' not in sound
    assert api.get('/api/library?audio_filter=issues').json()['total'] == 0
    assert api.get('/api/library?audio_filter=checked').json()['total'] == 1


def test_cancelled_lock_waiter_stops_before_resolution_or_analysis(api, monkeypatch):
    service = api.app.state.audio_service
    entered = threading.Event()
    original_inspect = service.inspect
    def waiting(*args, **kwargs):
        entered.set()
        return original_inspect(*args, **kwargs)
    monkeypatch.setattr(service, 'inspect', waiting)
    monkeypatch.setattr(service, 'resolve_sound', lambda _: pytest.fail('Cancelled waiter must not make metadata requests'))
    monkeypatch.setattr(quality, 'analyze_audio', lambda _: pytest.fail('Cancelled waiter must not decode audio'))

    service.processing.acquire()
    try:
        response = api.post('/api/audio-checks', json={'ids': ['sample']})
        assert response.status_code == 200
        identifier = response.json()['id']
        assert entered.wait(3)
        assert api.post('/api/audio-tasks/' + identifier + '/cancel').status_code == 200
    finally:
        service.processing.release()
    task = wait_terminal(api, identifier)
    assert task['status'] == 'cancelled'
    assert task['items'] == [] and task['completed'] == 0
    assert service.record('sample') is None


def test_cancel_between_source_resolution_and_analysis_does_not_decode(api, monkeypatch):
    service = api.app.state.audio_service
    path = api.app.state.library.file('sample')
    cancelled = threading.Event()
    def file_for(*args, **kwargs):
        cancelled.set()
        return path, None
    monkeypatch.setattr(service, 'file_for', file_for)
    monkeypatch.setattr(quality, 'analyze_audio', lambda _: pytest.fail('Cancellation after file resolution must stop decoding'))
    with pytest.raises(ValueError, match='取消'):
        service.inspect('sample', cancelled=cancelled.is_set)
    assert service.record('sample') is None
