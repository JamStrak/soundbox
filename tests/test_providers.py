import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from server import providers as p
from server.search import expand_query


SETTINGS = {
    "freesound_key": "private-freesound-secret",
    "freesound_client_id": "test-client",
    "elevenlabs_key": "private-eleven-secret",
    "text_api_key": "private-text-secret",
    "text_api_base": "https://text.example/v1",
    "text_model": "translator-model",
}
AUDIO = b"ID3\x04\x00\x00" + b"\x01" * 100


def raw_sound(**changes):
    return {
        "id": 42, "name": "Sword.wav", "tags": ["sword", "metal"],
        "description": "<p>Metal &amp; sword</p>", "duration": 2.25,
        "username": "recordist", "license": "https://creativecommons.org/licenses/by/3.0/",
        "url": "https://freesound.org/people/recordist/sounds/42/",
        "previews": {"preview-hq-mp3": "https://cdn.freesound.org/previews/0/42_100-hq.mp3"},
        "type": "wav", "created": "2026-09-13T00:00:00", "filesize": 4096,
        **changes,
    }


@pytest.fixture
def mock_http(monkeypatch):
    calls = []

    def install(handler):
        def dispatch(request):
            calls.append(request)
            return handler(request)

        monkeypatch.setattr(p, "_client", lambda **kwargs: httpx.Client(transport=httpx.MockTransport(dispatch), timeout=2, follow_redirects=False))
        return calls

    return install


def test_search_uses_current_endpoint_fields_filters_and_safe_metadata(mock_http):
    def handler(request):
        assert request.url.path == "/apiv2/search/"
        assert request.headers["Authorization"] == "Token private-freesound-secret"
        assert "token" not in request.url.params
        params = request.url.params
        assert params["query"] == "metal impact"
        assert params["filter"] == 'license:("Creative Commons 0" OR "Attribution") duration:[0 TO 3]'
        assert params["sort"] == "duration_asc"
        assert params["page"] == "2"
        assert {"previews", "license", "duration", "username"} <= set(params["fields"].split(","))
        return httpx.Response(200, json={"count": 31, "next": "https://freesound.org/apiv2/search/?page=3", "results": [raw_sound()]})

    calls = mock_http(handler)
    query = expand_query("金属撞击")
    result = p.search_freesound(query, SETTINGS, 2, "commercial", 3, "duration_asc")
    assert len(calls) == 1
    assert result["query"] is query
    assert result["total"] == 31 and result["has_more"]
    sound = result["items"][0]
    assert sound["id"] == "fs-42"
    assert sound["description"] == "Metal & sword"
    assert sound["license"] == "CC BY"
    assert sound["license_url"] == "https://creativecommons.org/licenses/by/3.0/"
    assert sound["preview_url"].startswith("https://cdn.freesound.org/")
    assert sound["waveform"] == []  # No invented audio waveform.
    assert not sound["saved"] and not sound["downloaded"]


def test_cc0_filter_rejects_unexpected_noncommercial_or_unknown_results(mock_http):
    def handler(request):
        assert request.url.params["filter"] == 'license:"Creative Commons 0"'
        return httpx.Response(200, json={"count": 3, "next": None, "results": [
            raw_sound(license="Creative Commons 0"),
            raw_sound(id=43, license="Attribution NonCommercial"),
            raw_sound(id=44, license="unknown"),
        ]})

    mock_http(handler)
    result = p.search_freesound(expand_query("sword"), SETTINGS, license_filter="cc0")
    assert [sound["source_id"] for sound in result["items"]] == ["42"]
    assert "隐藏" in result["warning"]


@pytest.mark.parametrize(("raw", "expected", "version"), [
    ("Attribution", "CC BY", "4.0"),
    ("Attribution NonCommercial", "CC BY-NC", "4.0"),
    ("Creative Commons 0", "CC0", "1.0"),
    ("http://creativecommons.org/licenses/by/3.0/", "CC BY", "3.0"),
    ("https://creativecommons.org/licenses/by-nc/4.0/", "CC BY-NC", "4.0"),
])
def test_license_normalization_preserves_version(raw, expected, version):
    label, url = p._license(raw)
    assert label == expected
    assert url.startswith("https://creativecommons.org/") and version in url


def test_forged_license_url_not_classified_as_commercial():
    assert p._license("https://bad.example/creativecommons.org/licenses/by/4.0/")[0] != "CC BY"


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_provider_http_errors_are_actionable_and_never_echo_secrets(mock_http, status):
    mock_http(lambda request: httpx.Response(status, json={"detail": "private-freesound-secret"}))
    with pytest.raises(p.ProviderError) as error:
        p.search_freesound(expand_query("sword"), SETTINGS)
    message = str(error.value)
    assert "Freesound" in message
    assert "private" not in message
    assert any(word in message for word in ["检查", "稍后"])


def test_network_timeout_does_not_include_request_url_or_headers(mock_http):
    def handler(request):
        raise httpx.ConnectTimeout("contains private-freesound-secret", request=request)

    mock_http(handler)
    with pytest.raises(p.ProviderError, match="超时") as error:
        p.search_freesound(expand_query("sword"), SETTINGS)
    assert error.value.status_code == 504
    assert "private" not in str(error.value)


def test_missing_key_fails_before_network(mock_http):
    calls = mock_http(lambda request: pytest.fail("must not make network call"))
    with pytest.raises(p.ProviderError, match="设置"):
        p.search_freesound(expand_query("sword"), {})
    assert calls == []


def test_detail_rejects_wrong_id_and_invalid_remote_preview(mock_http):
    calls = mock_http(lambda request: httpx.Response(200, json=raw_sound(id=43)))
    with pytest.raises(p.ProviderError, match="不一致"):
        p.freesound_detail("42", SETTINGS)
    assert calls[0].url.path == "/apiv2/sounds/42/"
    mock_http(lambda request: httpx.Response(200, json=raw_sound(previews={"preview-hq-mp3": "http://127.0.0.1/private"})))
    with pytest.raises(p.ProviderError, match="可信"):
        p.freesound_detail("42", SETTINGS)


@pytest.mark.parametrize("url", [
    "http://freesound.org/file.mp3", "https://freesound.org.evil.test/file.mp3",
    "https://evilfreesound.org/file.mp3", "https://127.0.0.1/file.mp3",
    "https://freesound.org@evil.test/file.mp3", "https://user:pass@freesound.org/file.mp3",
    "https://freesound.org:444/file.mp3", "https://freesound.org\\@evil.test/file.mp3",
    "file:///C:/private.mp3", "https://cdn.freesound.org/file.mp3#fragment",
])
def test_download_rejects_untrusted_urls_before_request(mock_http, url):
    calls = mock_http(lambda request: pytest.fail("must not reach network"))
    with pytest.raises(p.ProviderError, match="可信"):
        p.download_freesound({"source_id": "42", "preview_url": url}, SETTINGS)
    assert calls == []


def test_every_download_redirect_is_validated(mock_http):
    calls = mock_http(lambda request: httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}))
    with pytest.raises(p.ProviderError, match="可信"):
        p.download_freesound(p._sound(raw_sound()), SETTINGS)
    assert len(calls) == 1


def test_preview_download_has_no_secret_and_preserves_preview_format(mock_http):
    def handler(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, content=AUDIO, headers={"content-type": "audio/mpeg"})

    calls = mock_http(handler)
    content, extension, updates = p.download_freesound(p._sound(raw_sound()), SETTINGS)
    assert content == AUDIO and extension == "mp3" and updates == {}
    assert len(calls) == 1


def test_oauth_display_code_flow_uses_registered_redirect_and_form_exchange(mock_http):
    url = p.freesound_authorize_url(SETTINGS)
    params = parse_qs(urlsplit(url).query)
    assert params == {"client_id": ["test-client"], "response_type": ["code"]}
    assert "private" not in url

    def handler(request):
        assert request.url.path == "/apiv2/oauth2/access_token/"
        form = parse_qs(request.content.decode())
        assert form == {"client_id": ["test-client"], "client_secret": ["private-freesound-secret"], "grant_type": ["authorization_code"], "code": ["a-valid-code"]}
        return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600})

    mock_http(handler)
    updates = p.freesound_exchange_code(" a-valid-code ", SETTINGS)
    assert updates["freesound_access_token"] == "new-access"
    assert updates["freesound_refresh_token"] == "new-refresh"
    assert time.time() + 3500 < updates["freesound_expires_at"] < time.time() + 3700


def test_expired_oauth_refreshes_once_and_cdn_redirect_drops_bearer(mock_http):
    settings = {**SETTINGS, "freesound_access_token": "old-access", "freesound_refresh_token": "old-refresh", "freesound_expires_at": time.time() + 3600}
    attempts = 0

    def handler(request):
        nonlocal attempts
        if request.url.path.endswith("/access_token/"):
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["refresh_token"]
            assert form["refresh_token"] == ["old-refresh"]
            return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600})
        if request.url.host == "cdn.freesound.org":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=AUDIO, headers={"content-type": "audio/wav"})
        assert request.url.path == "/apiv2/sounds/42/download/"
        attempts += 1
        if attempts == 1:
            assert request.headers["authorization"] == "Bearer old-access"
            return httpx.Response(401, json={"detail": "Expired token"})
        assert request.headers["authorization"] == "Bearer new-access"
        return httpx.Response(302, headers={"location": "https://cdn.freesound.org/data/file.wav"})

    calls = mock_http(handler)
    content, extension, updates = p.download_freesound(p._sound(raw_sound()), settings, "original")
    assert content == AUDIO and extension == "wav"
    assert updates["freesound_refresh_token"] == "new-refresh"
    assert len(calls) == 4 and attempts == 2


def test_known_expired_token_refreshes_before_request_and_preserves_updates_on_failure(mock_http):
    settings = {**SETTINGS, "freesound_access_token": "old", "freesound_refresh_token": "refresh", "freesound_expires_at": 1}

    def handler(request):
        if request.url.path.endswith("/access_token/"):
            return httpx.Response(200, json={"access_token": "new", "refresh_token": "rotated", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer new"
        return httpx.Response(503)

    calls = mock_http(handler)
    with pytest.raises(p.ProviderError) as error:
        p.download_freesound(p._sound(raw_sound()), settings, "original")
    assert error.value.updated_tokens["freesound_refresh_token"] == "rotated"
    assert len(calls) == 2


def test_original_without_oauth_does_not_call_network(mock_http):
    calls = mock_http(lambda request: pytest.fail("must not reach network"))
    with pytest.raises(p.ProviderError, match="连接 Freesound 账号"):
        p.download_freesound(p._sound(raw_sound()), SETTINGS, "original")
    assert not calls


def test_original_after_preview_download_uses_original_format(mock_http):
    sound = p._sound(raw_sound())
    # Root replaces these two fields after saving the downloaded MP3 preview.
    sound.update(format="mp3", preview_url="/api/audio/fs-42")
    settings = {**SETTINGS, "freesound_access_token": "access"}
    mock_http(lambda request: httpx.Response(200, content=AUDIO, headers={"content-type": "audio/wav"}))
    _, extension, _ = p.download_freesound(sound, settings, "original")
    assert extension == "wav"


def test_preview_can_be_redownloaded_after_local_url_replacement(mock_http):
    sound = p._sound(raw_sound())
    sound.update(format="mp3", preview_url="/api/audio/fs-42")

    def handler(request):
        assert request.url == "https://cdn.freesound.org/previews/0/42_100-hq.mp3"
        return httpx.Response(200, content=AUDIO, headers={"content-type": "audio/mpeg"})

    mock_http(handler)
    assert p.download_freesound(sound, SETTINGS, "preview")[1] == "mp3"


@pytest.mark.parametrize("sound_id", ["fs-42", "freesound:42", "freesound-42", "42"])
def test_source_id_accepts_current_windows_safe_and_legacy_prefixes(sound_id):
    assert p._sound_id(sound_id) == "42"


def test_download_size_limit_checks_content_length(mock_http):
    mock_http(lambda request: httpx.Response(200, headers={"content-length": str(p.MAX_AUDIO_BYTES + 1)}, content=b""))
    with pytest.raises(p.ProviderError, match="50 MB"):
        p.download_freesound(p._sound(raw_sound()), SETTINGS)


def test_download_size_limit_checks_stream_without_content_length(mock_http, monkeypatch):
    monkeypatch.setattr(p, "MAX_AUDIO_BYTES", 100)
    mock_http(lambda request: httpx.Response(200, stream=httpx.ByteStream(b"x" * 101), headers={"content-type": "audio/mpeg"}))
    with pytest.raises(p.ProviderError, match="50 MB"):
        p.download_freesound(p._sound(raw_sound()), SETTINGS)


@pytest.mark.parametrize(("content", "content_type"), [(b"<html>error</html>", "text/html"), (b'{"error":1}', "application/octet-stream"), (b"", "audio/mpeg")])
def test_download_rejects_non_audio_or_empty_payload(mock_http, content, content_type):
    mock_http(lambda request: httpx.Response(200, content=content, headers={"content-type": content_type}))
    with pytest.raises(p.ProviderError):
        p.download_freesound(p._sound(raw_sound()), SETTINGS)


def test_elevenlabs_generation_uses_v2_loop_duration_and_one_paid_request(mock_http):
    def handler(request):
        assert request.method == "POST"
        assert request.url == "https://api.elevenlabs.io/v1/sound-generation?output_format=mp3_44100_128"
        assert request.headers["xi-api-key"] == "private-eleven-secret"
        body = json.loads(request.content)
        assert body["text"] == "金属撞击 heavy sword"
        assert body["duration_seconds"] == 30 and body["loop"] is True
        assert body["model_id"] == "eleven_text_to_sound_v2"
        return httpx.Response(200, content=AUDIO, headers={"content-type": "audio/mpeg"})

    calls = mock_http(handler)
    assert p.generate_elevenlabs("金属撞击 heavy sword", 30, True, SETTINGS) == (AUDIO, "mp3")
    assert len(calls) == 1


@pytest.mark.parametrize("duration", [0, 0.49, 30.1, -1, float("nan"), float("inf"), True])
def test_elevenlabs_rejects_bad_duration_before_paid_call(mock_http, duration):
    calls = mock_http(lambda request: pytest.fail("must not reach network"))
    with pytest.raises(p.ProviderError, match="0.5"):
        p.generate_elevenlabs("Sword impact", duration, False, SETTINGS)
    assert not calls


def test_paid_generation_timeout_is_not_automatically_retried(mock_http):
    def handler(request):
        raise httpx.ReadTimeout("private-eleven-secret", request=request)

    calls = mock_http(handler)
    with pytest.raises(p.ProviderError, match="未自动重试") as error:
        p.generate_elevenlabs("Sword impact", 1, False, SETTINGS)
    assert len(calls) == 1
    assert "private" not in str(error.value)


def test_elevenlabs_connection_check_is_read_only(mock_http):
    def handler(request):
        assert request.method == "GET" and request.url.path == "/v1/user"
        return httpx.Response(200, json={"user_id": "test-user"})

    calls = mock_http(handler)
    assert p.test_connection("elevenlabs", SETTINGS)["ok"]
    assert len(calls) == 1


def test_text_connection_check_lists_models_without_completions(mock_http):
    def handler(request):
        assert request.method == "GET" and request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "translator-model"}]})

    calls = mock_http(handler)
    assert p.test_connection("text", SETTINGS)["ok"]
    assert len(calls) == 1


def test_text_connection_unlisted_model_not_reported_as_success(mock_http):
    mock_http(lambda request: httpx.Response(200, json={"data": [{"id": "other-model"}]}))
    result = p.test_connection("text", SETTINGS)
    assert not result["ok"]
    assert "模型" in result["message"]


def test_optional_ai_translation_uses_configured_model_and_actual_query(mock_http):
    def handler(request):
        assert request.url == "https://text.example/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer private-text-secret"
        body = json.loads(request.content)
        assert body["model"] == "translator-model"
        assert body["messages"][-1]["content"] == "厚重的铁剑击中盾牌"
        return httpx.Response(200, json={"choices": [{"message": {"content": "heavy sword impact"}}]})

    mock_http(handler)
    result = p.translate_query("厚重的铁剑击中盾牌", SETTINGS, use_ai=True)
    assert result["translated"] == "heavy sword impact"
    assert result["method"] == "文本 AI 整理"
    assert result["original"] == "厚重的铁剑击中盾牌"


def test_ai_failure_falls_back_explicitly_without_key_leak(mock_http):
    mock_http(lambda request: httpx.Response(401, json={"error": "private-text-secret"}))
    result = p.translate_query("魔法泡芙", SETTINGS, use_ai=True)
    assert result["terms"] == ["magic", "泡芙"]
    assert "已回退" in result["warning"] and "词库未覆盖" in result["warning"]
    assert "private" not in str(result)


@pytest.mark.parametrize("content", ["解释：这里是一个声音", "", "word " * 30, "private-text-secret"])
def test_invalid_ai_output_never_claims_translation_success(mock_http, content):
    mock_http(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": content}}]}))
    result = p.translate_query("脚步", SETTINGS, use_ai=True)
    assert result["translated"] == "footstep"
    assert "已回退" in result["warning"]
    assert "private" not in str(result)


def test_without_ai_translation_does_not_contact_api(mock_http):
    calls = mock_http(lambda request: pytest.fail("must not reach network"))
    assert p.translate_query("金属", SETTINGS)["translated"] == "metal"
    assert not calls


@pytest.mark.parametrize("base", ["https://api.example/v1/", "http://127.0.0.1:24159/v1", "http://localhost:24159/v1", "http://[::1]:24159/v1"])
def test_text_base_allows_https_or_explicit_local_http(base):
    assert p.validate_text_api_base(base) == base.rstrip("/")


@pytest.mark.parametrize("base", ["http://api.example/v1", "http://192.168.1.2/v1", "http://localhost.evil.test/v1", "https://user:secret@api.example/v1", "https://api.example/v1?key=secret", "file:///C:/private"])
def test_text_base_rejects_cleartext_remote_or_embedded_credentials(base):
    with pytest.raises(p.ProviderError):
        p.validate_text_api_base(base)


def test_malformed_json_is_actionable(mock_http):
    mock_http(lambda request: httpx.Response(200, content=b"not json"))
    with pytest.raises(p.ProviderError, match="无效数据"):
        p.search_freesound(expand_query("click"), SETTINGS)
