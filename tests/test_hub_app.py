"""Real FastAPI adapter with isolated data; no provider/network requests."""
import io
import os
import threading
import time
import wave
import pytest
from fastapi.testclient import TestClient

# The collection environment must isolate even the module's default app.
assert os.environ.get('SOUNDBOX_DATA_DIR'), 'Set isolated SOUNDBOX_DATA_DIR before collecting tests'
from server.app import create_app
from server import providers


@pytest.fixture
def application(tmp_path, monkeypatch):
    monkeypatch.setenv('SOUNDBOX_NO_SEED', '1')
    app = create_app(tmp_path)
    app.state.hub_lifecycle.shutdown = lambda: None
    return app


def registered(app):
    runtime = app.state.hub_lifecycle
    runtime.session(dict(sessionId='test_page_01', action='heartbeat'))
    return {'X-Soundbox-Client': 'local', 'X-Hub-Session': 'test_page_01'}


def test_real_app_hub_auth_bridge_and_session_bypass_legacy_header(application):
    with TestClient(application) as client:
        status = client.get('/api/hub/status').json()
        assert status['toolId'] == 'soundbox'
        assert client.post('/api/hub/session', headers={'X-Hub-Control': status['controlToken']},
                           json={'sessionId':'test_page_01', 'action':'heartbeat'}).status_code == 200
        bridge = client.get('/api/hub/bridge.js')
        assert bridge.status_code == 200 and '__creatorHubBridge' in bridge.text
        assert 'application/javascript' in bridge.headers['content-type']


def test_quality_job_stays_busy_after_submit_response(application):
    finish = threading.Event()
    try:
        application.state.audio_service.start('fixture', 'fixture', 1, lambda stop, update: finish.wait(5))
        status = application.state.hub_lifecycle.snapshot()
        assert status['activeJobs'] + status['queuedJobs'] == 1
        assert status['taskState'] == 'busy'
    finally:
        finish.set()
        deadline = time.monotonic() + 3
        while application.state.hub_lifecycle.snapshot()['taskState'] == 'busy' and time.monotonic() < deadline:
            time.sleep(.01)


def test_generation_job_tracks_provider_and_persistence(application, monkeypatch):
    entered, finish = threading.Event(), threading.Event()
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
        wav.writeframes(b'\x00\x00' * 1600)
    def fake_provider(*args):
        entered.set(); finish.wait(5)
        return output.getvalue(), 'wav'
    monkeypatch.setattr(providers, 'generate_elevenlabs', fake_provider)
    # This temporary fixture value is not a user's key and is never transmitted.
    application.state.settings.update({'elevenlabs_key':'fixture-not-a-provider-key'})
    try:
        with TestClient(application, headers=registered(application)) as client:
            response = client.post('/api/generate', json={'prompt':'fixture', 'duration':1, 'loop':False})
            assert response.status_code == 200 and entered.wait(2)
            assert application.state.hub_lifecycle.snapshot()['activeJobs'] == 1
            finish.set()
            deadline = time.monotonic() + 4
            while application.state.hub_lifecycle.snapshot()['activeJobs'] and time.monotonic() < deadline:
                time.sleep(.01)
            assert application.state.hub_lifecycle.snapshot()['activeJobs'] == 0
    finally:
        finish.set()


def test_prepare_blocks_real_business_endpoint(application):
    runtime = application.state.hub_lifecycle
    headers = registered(application)
    runtime.session(dict(sessionId='test_page_01', action='release'))
    with TestClient(application, headers=headers) as client:
        state = client.get('/api/hub/status').json()
        result = client.post('/api/hub/control', headers={'X-Hub-Control':state['controlToken']},
            json=dict(action='prepareShutdown', instanceId=state['instanceId'], revision=state['revision']))
        assert result.status_code == 200
        assert client.post('/api/folders', json={'name':'must-not-create'}).status_code == 503
        assert application.state.library.folders() == []
