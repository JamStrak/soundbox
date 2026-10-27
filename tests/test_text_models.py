"""Dynamic model discovery and credential isolation, with no external requests."""
import json

import httpx
from fastapi.testclient import TestClient
import pytest

from server import providers
from server.app import create_app
from server.settings import Settings


HEADERS = {"X-Soundbox-Client": "local"}
STORED_BASE = "https://stored-text.example/v1"
STORED_KEY = "mock-saved-text-secret"
NEW_KEY = "mock-new-text-secret"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SOUNDBOX_NO_SEED", "1")
    application = create_app(tmp_path)
    application.state.settings.update({
        "text_api_base": STORED_BASE, "text_api_key": STORED_KEY, "text_model": "",
    })
    with TestClient(application, headers=HEADERS) as connection:
        yield connection


@pytest.fixture
def mock_http(monkeypatch):
    calls = []

    def install(handler):
        def dispatch(request):
            calls.append(request)
            return handler(request)

        monkeypatch.setattr(providers, "_client", lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(dispatch), timeout=2, follow_redirects=False,
        ))
        return calls

    return install


def snapshot(client):
    settings = client.app.state.settings
    return settings.get(), settings.path.read_bytes()


def assert_unchanged(client, before):
    assert snapshot(client) == before
    assert Settings(client.app.state.library.directory).get() == before[0]


def test_new_key_discovers_all_dynamic_models_without_selected_model_or_saving(client, mock_http):
    ids = [f"vendor/model-{number}-future" for number in range(64)]
    models = [{"id": identifier, "owned_by": "vendor", "unexpected_secret": NEW_KEY} for identifier in ids]
    models.extend([
        {"id": ids[0]}, {"id": "  " + ids[1] + "  "}, {"id": ""}, {"id": "   "},
        {"id": "bad\nmodel"}, {"id": "x" * 301}, {"id": 23}, {"id": None}, {}, None, 42, "bad row",
    ])

    def handler(request):
        assert request.method == "GET"
        assert str(request.url) == STORED_BASE + "/models"
        assert request.headers["authorization"] == "Bearer " + NEW_KEY
        assert "key" not in request.url.params and "token" not in request.url.params
        return httpx.Response(200, json={"data": models})

    calls = mock_http(handler)
    before = snapshot(client)
    assert before[0]["text_model"] == ""
    response = client.post("/api/settings/text-models", json={"text_api_base": STORED_BASE, "text_api_key": NEW_KEY})
    assert response.status_code == 200
    assert response.json() == {"items": [{"id": identifier, "owned_by": "vendor"} for identifier in ids], "total": 64}
    assert NEW_KEY not in response.text and STORED_KEY not in response.text
    assert len(calls) == 1
    assert_unchanged(client, before)


@pytest.mark.parametrize("key_payload", [{}, {"text_api_key": ""}, {"text_api_key": "   "}])
def test_same_base_blank_key_reuses_saved_key_without_mutating_settings(client, mock_http, key_payload):
    def handler(request):
        assert request.headers["authorization"] == "Bearer " + STORED_KEY
        assert str(request.url) == STORED_BASE + "/models"
        return httpx.Response(200, json={"data": [{"id": "current-live-model"}]})

    calls = mock_http(handler)
    before = snapshot(client)
    response = client.post("/api/settings/text-models", json={"text_api_base": STORED_BASE + "/", **key_payload})
    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == "current-live-model"
    assert len(calls) == 1
    assert_unchanged(client, before)


@pytest.mark.parametrize("failure", ["empty", "malformed", "bad_json", "401", "429", "network"])
def test_discovery_errors_never_echo_secrets_or_save_draft_credentials(client, mock_http, failure):
    def handler(request):
        assert request.headers["authorization"] == "Bearer " + NEW_KEY
        if failure == "network":
            raise httpx.ReadTimeout("upstream debug " + NEW_KEY, request=request)
        if failure == "empty":
            return httpx.Response(200, json={"data": [], "debug": NEW_KEY})
        if failure == "malformed":
            return httpx.Response(200, json={"data": {"key": NEW_KEY}})
        if failure == "bad_json":
            return httpx.Response(200, content=("not json " + NEW_KEY).encode())
        return httpx.Response(int(failure), json={"error": {"detail": NEW_KEY}})

    calls = mock_http(handler)
    before = snapshot(client)
    response = client.post("/api/settings/text-models", json={"text_api_base": "https://new-text.example/v1", "text_api_key": NEW_KEY})
    assert response.status_code >= 400
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]
    assert NEW_KEY not in response.text and STORED_KEY not in response.text
    assert len(calls) == 1
    assert_unchanged(client, before)


def test_cross_base_without_new_key_refuses_remote_request_and_never_sends_saved_key(client, mock_http):
    calls = mock_http(lambda request: pytest.fail("saved key must never be sent to the new host"))
    before = snapshot(client)
    response = client.post("/api/settings/text-models", json={"text_api_base": "https://new-text.example/v1", "text_api_key": ""})
    assert response.status_code == 400 and "Key" in response.json()["detail"]
    assert STORED_KEY not in response.text
    assert not calls
    assert_unchanged(client, before)


def test_cross_base_explicit_new_key_lists_models_but_original_settings_remain(client, mock_http):
    def handler(request):
        assert str(request.url) == "https://new-text.example/v1/models"
        assert request.headers["authorization"] == "Bearer " + NEW_KEY
        assert STORED_KEY not in str(request.headers)
        return httpx.Response(200, json={"data": [{"id": "another-provider/brand-new-model"}]})

    calls = mock_http(handler)
    before = snapshot(client)
    response = client.post("/api/settings/text-models", json={"text_api_base": "https://new-text.example/v1", "text_api_key": NEW_KEY})
    assert response.status_code == 200 and response.json()["total"] == 1
    assert NEW_KEY not in response.text and STORED_KEY not in response.text
    assert len(calls) == 1
    assert_unchanged(client, before)


def test_cross_base_local_server_receives_no_saved_bearer(client, mock_http):
    def handler(request):
        assert str(request.url) == "http://127.0.0.1:23978/v1/models"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"data": [{"id": "local-model"}]})

    mock_http(handler)
    before = snapshot(client)
    response = client.post("/api/settings/text-models", json={"text_api_base": "http://127.0.0.1:23978/v1"})
    assert response.status_code == 200
    assert_unchanged(client, before)


def test_patch_cross_base_without_replacement_key_is_rejected_before_persistence(client, mock_http):
    calls = mock_http(lambda request: pytest.fail("settings patch must not call providers"))
    before = snapshot(client)
    response = client.patch("/api/settings", json={"text_api_base": "https://new-text.example/v1", "text_model": "new-model"})
    assert response.status_code == 400 and "切换" in response.json()["detail"]
    assert not calls
    assert_unchanged(client, before)


def test_selected_dynamic_model_and_replacement_settings_persist_only_on_patch(client, mock_http):
    selected = "provider/new-model-2029"
    mock_http(lambda request: httpx.Response(200, json={"data": [{"id": selected}]}))
    fetched = client.post("/api/settings/text-models", json={"text_api_base": "https://new-text.example/v1", "text_api_key": NEW_KEY})
    assert fetched.status_code == 200
    assert client.app.state.settings.get()["text_model"] == ""
    response = client.patch("/api/settings", json={
        "text_api_base": "https://new-text.example/v1", "text_api_key": NEW_KEY,
        "text_model": fetched.json()["items"][0]["id"], "ai_translation": True,
    })
    assert response.status_code == 200
    assert response.json()["text_model"] == selected and response.json()["text_api_key_set"]
    assert NEW_KEY not in response.text
    saved = Settings(client.app.state.library.directory).get()
    assert saved["text_model"] == selected and saved["text_api_key"] == NEW_KEY
    assert saved["text_api_base"] == "https://new-text.example/v1" and saved["ai_translation"] is True


def test_request_validation_does_not_echo_invalid_secret(client, mock_http):
    calls = mock_http(lambda request: pytest.fail("invalid TextModels request must not reach upstream"))
    secret = "sensitive-invalid-key-" * 60
    before = snapshot(client)
    response = client.post("/api/settings/text-models", json={"text_api_base": STORED_BASE, "text_api_key": secret})
    assert response.status_code == 422
    assert secret not in response.text and "sensitive-invalid-key-" not in response.text
    assert not calls
    assert_unchanged(client, before)


def test_selected_model_is_used_and_thinking_is_disabled_only_for_deepseek(mock_http):
    selected = "discovered-new-model"

    def handler(request):
        body = json.loads(request.content)
        assert request.method == "POST" and request.url.path == "/v1/chat/completions"
        assert body["model"] == selected
        assert request.headers["authorization"] == "Bearer " + NEW_KEY
        if request.url.host == "api.deepseek.com":
            assert body["thinking"] == {"type": "disabled"}
        else:
            assert "thinking" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": "sword impact"}}]})

    calls = mock_http(handler)
    for base in ("https://api.deepseek.com/v1", "https://custom-text.example/v1"):
        result = providers.translate_query("刀剑撞击", {"text_api_base": base, "text_api_key": NEW_KEY, "text_model": selected}, use_ai=True)
        assert result["translated"] == "sword impact"
        assert result["method"] == "文本 AI 整理"
    assert len(calls) == 2
