"""Regressions for OAuth rotation, download quality, and paid-query caching.

Only provider calls are simulated. HTTP routing, locks, audio persistence and
encrypted settings use the real application in an isolated temporary directory.
"""
from concurrent.futures import ThreadPoolExecutor
import io
import threading
import time
import wave

from fastapi.testclient import TestClient
import pytest

from server import providers
from server.app import create_app
from server.audio import base_sound
from server.settings import Settings


HEADERS = {"X-Soundbox-Client": "local"}


def audio_bytes():
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(44100)
        stream.writeframes(b"\0\0" * 4410)
    return output.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SOUNDBOX_NO_SEED", "1")
    application = create_app(tmp_path)
    application.state.settings.update({
        "freesound_key": "mock-api-key", "freesound_client_id": "mock-client",
        "freesound_access_token": "expired-access", "freesound_refresh_token": "old-refresh",
        "freesound_expires_at": 1,
    })
    for sound_id in ("42", "43"):
        sound = base_sound("fs-" + sound_id, "Sword " + sound_id, "freesound")
        sound.update({
            "source_id": sound_id, "quality": "preview", "saved": True, "downloaded": False,
            "format": "wav", "original_format": "wav",
            "preview_url": "https://cdn.freesound.org/previews/" + sound_id + ".mp3",
            "remote_preview_url": "https://cdn.freesound.org/previews/" + sound_id + ".mp3",
        })
        application.state.library.put(sound)

    # Observe the second actual HTTP request reaching the app, not just a task
    # being submitted. A gate in the fake provider keeps the first in progress.
    application.state.second_download_requested = threading.Event()
    observed_downloads = 0
    observed_lock = threading.Lock()

    @application.middleware("http")
    async def observe_download(request, call_next):
        nonlocal observed_downloads
        if request.method == "POST" and request.url.path.endswith("/download"):
            with observed_lock:
                observed_downloads += 1
                if observed_downloads == 2:
                    application.state.second_download_requested.set()
        return await call_next(request)

    with TestClient(application, headers=HEADERS) as connection:
        yield connection


def test_parallel_original_downloads_refresh_once_and_reuse_rotated_tokens(client, monkeypatch):
    first_entered = threading.Event()
    second_provider_entered = threading.Event()
    release_first = threading.Event()
    record_lock = threading.Lock()
    seen_refresh_tokens = []
    refreshes = []
    payload = audio_bytes()

    def download(sound, configuration, quality):
        assert quality == "original"
        with record_lock:
            seen_refresh_tokens.append(configuration["freesound_refresh_token"])
            position = len(seen_refresh_tokens)
        if position == 1:
            first_entered.set()
            assert release_first.wait(5), "first simulated download was not released"
        else:
            second_provider_entered.set()
        updates = {}
        if configuration["freesound_refresh_token"] == "old-refresh":
            refreshes.append(1)
            updates = {"freesound_access_token": "new-access", "freesound_refresh_token": "rotated-refresh", "freesound_expires_at": time.time() + 3600}
        return payload, "wav", updates

    monkeypatch.setattr(providers, "download_freesound", download)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, "/api/sounds/fs-42/download", json={"quality": "original"})
        try:
            assert first_entered.wait(5)
            second = pool.submit(client.post, "/api/sounds/fs-43/download", json={"quality": "original"})
            assert client.app.state.second_download_requested.wait(5)
            assert not second_provider_entered.wait(.15), "second download used the unrotated credentials concurrently"
        finally:
            release_first.set()
        responses = [first.result(timeout=10), second.result(timeout=10)]

    assert [response.status_code for response in responses] == [200, 200]
    assert seen_refresh_tokens == ["old-refresh", "rotated-refresh"]
    assert len(refreshes) == 1
    assert all(response.json()["quality"] == "original" for response in responses)
    assert Settings(client.app.state.library.directory).get()["freesound_refresh_token"] == "rotated-refresh"


def test_pending_preview_after_original_keeps_original_file_and_quality(client, monkeypatch):
    original_entered = threading.Event()
    preview_provider_entered = threading.Event()
    release_original = threading.Event()
    calls = []
    payload = audio_bytes()

    def download(sound, configuration, quality):
        calls.append(quality)
        if quality == "original":
            original_entered.set()
            assert release_original.wait(5), "original simulated download was not released"
        else:
            preview_provider_entered.set()
        return payload, "wav", {}

    monkeypatch.setattr(providers, "download_freesound", download)
    with ThreadPoolExecutor(max_workers=2) as pool:
        original = pool.submit(client.post, "/api/sounds/fs-42/download", json={"quality": "original"})
        try:
            assert original_entered.wait(5)
            preview = pool.submit(client.post, "/api/sounds/fs-42/download", json={"quality": "preview"})
            assert client.app.state.second_download_requested.wait(5)
            assert not preview_provider_entered.wait(.15)
        finally:
            release_original.set()
        responses = [original.result(timeout=10), preview.result(timeout=10)]

    assert [response.status_code for response in responses] == [200, 200]
    assert calls == ["original"]
    assert all(response.json()["quality"] == "original" for response in responses)
    library = client.app.state.library
    assert library.get("fs-42")["quality"] == "original"
    assert library.file("fs-42").read_bytes() == payload


def test_failed_download_still_persists_rotated_oauth_without_exposing_tokens(client, monkeypatch):
    def fail_after_refresh(sound, configuration, quality):
        error = providers.ProviderError("Freesound 服务暂时不可用", 502)
        error.updated_tokens = {
            "freesound_access_token": "new-access-after-failure",
            "freesound_refresh_token": "rotated-after-failure",
            "freesound_expires_at": time.time() + 3600,
        }
        raise error

    monkeypatch.setattr(providers, "download_freesound", fail_after_refresh)
    response = client.post("/api/sounds/fs-42/download", json={"quality": "original"})
    assert response.status_code == 502
    assert response.json() == {"detail": "Freesound 服务暂时不可用"}
    persisted = Settings(client.app.state.library.directory).get()
    assert persisted["freesound_access_token"] == "new-access-after-failure"
    assert persisted["freesound_refresh_token"] == "rotated-after-failure"
    assert client.app.state.library.file("fs-42") is None
    assert "rotated-after-failure" not in client.get("/api/settings").text


def test_ai_query_translation_is_cached_across_pages_and_filters_until_settings_change(client, monkeypatch):
    client.app.state.settings.update({
        "ai_translation": True, "text_api_key": "mock-text-key",
        "text_api_base": "https://text.example/v1", "text_model": "model-one",
    })
    translations = []
    searches = []

    def translate(text, configuration, use_ai=False):
        assert use_ai is True
        translations.append((text, configuration["text_model"]))
        return {"original": text, "translated": "sword impact", "terms": ["sword", "impact"], "method": "文本 AI 整理"}

    def search(query, configuration, page, license_filter, duration_max, sort):
        searches.append((page, license_filter, duration_max, sort))
        return {"items": [], "total": 0, "page": page, "has_more": False, "query": query}

    monkeypatch.setattr(providers, "translate_query", translate)
    monkeypatch.setattr(providers, "search_freesound", search)
    for params in ({"page": 1}, {"page": 2}, {"page": 1, "license": "cc0"}, {"duration_max": 3, "sort": "duration_asc"}):
        response = client.get("/api/search", params={"q": "刀剑撞击", "provider": "freesound", "use_ai": True, **params})
        assert response.status_code == 200
        assert response.json()["query"]["translated"] == "sword impact"
    assert translations == [("刀剑撞击", "model-one")]
    assert len(searches) == 4

    assert client.patch("/api/settings", json={"text_model": "model-two"}).status_code == 200
    assert client.get("/api/search", params={"q": "刀剑撞击", "provider": "freesound", "use_ai": True}).status_code == 200
    assert translations == [("刀剑撞击", "model-one"), ("刀剑撞击", "model-two")]


def test_search_requires_explicit_ai_opt_in_even_with_legacy_enabled_setting(client, monkeypatch):
    client.app.state.settings.update({'ai_translation': True})
    calls = []

    def translate(text, configuration, use_ai=False):
        calls.append(use_ai)
        return {'original': text, 'translated': 'sword', 'terms': ['sword'], 'method': 'test'}

    monkeypatch.setattr(providers, 'translate_query', translate)
    monkeypatch.setattr(providers, 'search_freesound', lambda query, *_: {
        'items': [], 'total': 0, 'page': 1, 'has_more': False, 'query': query})
    for extra in ({}, {'use_ai': False}, {'use_ai': True}, {'use_ai': False}):
        response = client.get('/api/search', params={'provider': 'freesound', 'q': 'sword', **extra})
        assert response.status_code == 200
    assert calls == [False, False, True, False]
