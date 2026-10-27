"""Anonymous API fixtures: no live requests, credentials, or billable actions."""
import time

import httpx
import pytest

from server import openverse as ov
from server import providers
from server.search import expand_query


ID = "39918cd2-10ee-45a1-affd-eac4b2ec571f"
ID_TWO = "e351a913-07a9-4108-8a03-30ea16a9d9f1"
PREVIEW = "https://cdn.freesound.org/previews/530/530486_2532248-hq.mp3"
AUDIO = b"ID3\x04\x00\x00" + b"\x01" * 100


def raw_sound(**changes):
    return {
        "id": ID, "title": "Metal Impact", "indexed_on": "2024-05-21T15:00:07.873258Z",
        "foreign_landing_url": "https://freesound.org/people/Rickplayer/sounds/530486",
        "url": PREVIEW, "creator": "Rickplayer", "license": "cc0", "license_version": "1.0",
        "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
        "provider": "freesound", "source": "freesound", "category": None,
        "duration": 1790, "filetype": "mp3", "tags": [{"name": "Metal"}, {"name": "impact"}],
        "alt_files": [{"url": "https://freesound.org/apiv2/sounds/530486/download/", "filetype": "wav"}],
        "attribution": '"Metal Impact" by Rickplayer is marked with CC0 1.0.',
        **changes,
    }


def page(items=None, count=240, pages=12):
    return {"result_count": count, "page_count": pages, "page_size": 20, "page": 1, "results": items if items is not None else [raw_sound()]}


@pytest.fixture(autouse=True)
def empty_caches():
    with ov._CACHE_LOCK:
        ov._SEARCH_CACHE.clear()
        ov._DETAIL_CACHE.clear()
    yield
    with ov._CACHE_LOCK:
        ov._SEARCH_CACHE.clear()
        ov._DETAIL_CACHE.clear()


@pytest.fixture
def mock_http(monkeypatch):
    calls = []

    def install(handler):
        def dispatch(request):
            calls.append(request)
            assert "authorization" not in request.headers
            assert "xi-api-key" not in request.headers
            assert "token" not in request.url.params
            return handler(request)

        monkeypatch.setattr(providers, "_client", lambda **kwargs: httpx.Client(transport=httpx.MockTransport(dispatch), timeout=2, follow_redirects=False))
        return calls

    return install


def test_anonymous_search_uses_freesound_index_and_converts_milliseconds(mock_http):
    def handler(request):
        assert request.url.host == "api.openverse.org"
        assert request.url.path == "/v1/audio/"
        assert dict(request.url.params) == {"q": "metal impact", "source": "freesound", "page": "1", "page_size": "20", "filter_dead": "true"}
        return httpx.Response(200, json=page())

    calls = mock_http(handler)
    result = ov.search_openverse(expand_query("金属撞击"))
    sound = result["items"][0]
    assert len(calls) == 1
    assert sound["id"] == "ov-" + ID
    assert sound["source"] == "openverse" and sound["origin_source"] == "freesound"
    assert sound["duration"] == 1.79
    assert sound["license"] == "CC0" and sound["tags"] == ["Metal", "impact"]
    assert sound["attribution"] == raw_sound()["attribution"]
    assert sound["preview_url"] == PREVIEW and sound["remote_preview_url"] == PREVIEW
    assert sound["quality"] == "preview" and sound["download_label"] == "公开 MP3"
    assert sound["format"] == "mp3" and sound["waveform"] == []
    assert "alt_files" not in sound
    assert result["has_more"]
    assert "Freesound" in result["pagination_note"] and "240" in result["pagination_note"]


@pytest.mark.parametrize(("choice", "expected"), [("cc0", "cc0"), ("commercial", "cc0,by")])
def test_license_filter_is_sent_upstream_and_bad_results_hidden(mock_http, choice, expected):
    def handler(request):
        assert request.url.params["license"] == expected
        return httpx.Response(200, json=page([raw_sound(), raw_sound(id=ID_TWO, license="by-nc", license_version="4.0")]))

    mock_http(handler)
    result = ov.search_openverse(expand_query("impact"), license_filter=choice)
    assert [sound["source_id"] for sound in result["items"]] == [ID]
    assert "隐藏" in result["warning"]


def test_license_name_and_version_preserved():
    sound = ov._sound(raw_sound(license="by-nc", license_version="4.0", license_url="https://creativecommons.org/licenses/by-nc/4.0/"))
    assert sound["license"] == "CC BY-NC 4.0"
    assert sound["license_url"].endswith("/by-nc/4.0/")


def test_duration_and_sort_only_affect_current_page_with_clear_warning(mock_http):
    def handler(request):
        assert "length" not in request.url.params
        assert "sort" not in request.url.params
        assert "unstable__sort_by" not in request.url.params
        return httpx.Response(200, json=page([
            raw_sound(duration=5000), raw_sound(id=ID_TWO, duration=1000),
            raw_sound(id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", duration=None),
        ]))

    calls = mock_http(handler)
    result = ov.search_openverse(expand_query("impact"), duration_max=2, sort="duration_asc")
    assert [sound["source_id"] for sound in result["items"]] == [ID_TWO]
    assert result["total"] == 240 and result["has_more"]
    assert "仅筛选当前页" in result["warning"] and "仅按本页时长排序" in result["warning"]
    result = ov.search_openverse(expand_query("impact"), sort="duration_desc")
    assert [sound["duration"] for sound in result["items"]] == [5, 1, 0]
    assert len(calls) == 1  # Local filters do not spend extra anonymous quota.


def test_upstream_total_remains_accurate_while_anonymous_pages_stop_at_twelve(mock_http):
    mock_http(lambda request: httpx.Response(200, json=page(count=815, pages=41)))
    result = ov.search_openverse(expand_query("impact"), page=12)
    assert result["total"] == 815
    assert result["page"] == 12 and result["has_more"] is False
    assert "240" in result["pagination_note"]
    with pytest.raises(providers.ProviderError, match="12 页"):
        ov.search_openverse(expand_query("impact"), page=13)


def test_local_filter_warning_does_not_hide_unknown_chinese_warning(mock_http):
    mock_http(lambda request: httpx.Response(200, json=page()))
    query = expand_query("魔法泡芙")
    result = ov.search_openverse(query, duration_max=3)
    assert "词库未覆盖" in result["warning"] and "泡芙" in result["warning"]
    assert "仅筛选当前页" in result["warning"]
    assert result["query"]["warning"] == query["warning"]


def test_search_and_detail_cache_are_deep_copies_and_expire_after_ten_minutes(mock_http):
    calls = mock_http(lambda request: httpx.Response(200, json=raw_sound() if request.url.path != "/v1/audio/" else page()))
    query = expand_query("impact")
    result = ov.search_openverse(query)
    result["items"][0]["tags"].append("pollution")
    result["query"]["terms"].append("pollution")
    sound = ov.openverse_detail(ID)
    assert sound["tags"] == ["Metal", "impact"]
    sound["tags"].append("other pollution")
    assert ov.openverse_detail(ID)["tags"] == ["Metal", "impact"]
    result = ov.search_openverse(query)
    assert "pollution" not in str(result)
    assert len(calls) == 1
    assert ov.CACHE_SECONDS == 600
    with ov._CACHE_LOCK:
        key = next(iter(ov._SEARCH_CACHE))
        _, data = ov._SEARCH_CACHE[key]
        ov._SEARCH_CACHE[key] = (time.monotonic() - 1, data)
        _, data = ov._DETAIL_CACHE[ID]
        ov._DETAIL_CACHE[ID] = (time.monotonic() - 1, data)
    ov.openverse_detail(ID)
    ov.search_openverse(query)
    assert len(calls) == 3


def test_search_cache_is_bounded_and_evicts_oldest_query(mock_http, monkeypatch):
    assert ov.CACHE_KEYS == 128
    monkeypatch.setattr(ov, "CACHE_KEYS", 2)
    calls = mock_http(lambda request: httpx.Response(200, json=page()))
    for term in ["sword", "rain", "wind"]:
        ov.search_openverse(expand_query(term))
    assert len(ov._SEARCH_CACHE) == 2
    ov.search_openverse(expand_query("sword"))
    assert len(calls) == 4


def test_detail_uses_canonical_id_and_rejects_wrong_response(mock_http):
    def handler(request):
        assert request.url.path == f"/v1/audio/{ID}/"
        return httpx.Response(200, json=raw_sound(id=ID_TWO))

    mock_http(handler)
    with pytest.raises(providers.ProviderError, match="不一致"):
        ov.openverse_detail("ov-" + ID)


def test_download_resolves_by_id_and_ignores_all_client_urls_and_alt_files(mock_http):
    def handler(request):
        if request.url.host == "api.openverse.org":
            return httpx.Response(200, json=raw_sound())
        assert str(request.url) == PREVIEW
        return httpx.Response(200, content=AUDIO, headers={"content-type": "audio/mpeg"})

    calls = mock_http(handler)
    body = {"id": "ov-" + ID, "source_id": ID, "preview_url": "http://127.0.0.1/private", "remote_preview_url": "https://freesound.org/apiv2/sounds/530486/download/"}
    content, extension = ov.download_openverse(body)
    assert content == AUDIO and extension == "mp3"
    assert len(calls) == 2
    assert all("download/" not in request.url.path for request in calls)
    assert all("127.0.0.1" not in str(request.url) for request in calls)


def test_download_after_search_reuses_verified_metadata_without_extra_api_call(mock_http):
    def handler(request):
        if request.url.host == "api.openverse.org":
            assert request.url.path == "/v1/audio/"
            return httpx.Response(200, json=page())
        return httpx.Response(200, content=AUDIO, headers={"content-type": "audio/mpeg"})

    calls = mock_http(handler)
    sound = ov.search_openverse(expand_query("impact"))["items"][0]
    sound["remote_preview_url"] = "/api/audio/ov-local"
    assert ov.download_openverse(sound) == (AUDIO, "mp3")
    assert len(calls) == 2


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/private.mp3", "http://cdn.freesound.org/previews/530/530486_2532248-hq.mp3",
    "https://cdn.freesound.org.evil.test/previews/530/530486_2532248-hq.mp3",
    "https://freesound.org/apiv2/sounds/530486/download/",
    "https://cdn.freesound.org/previews/../apiv2/download.mp3",
    "https://name:secret@cdn.freesound.org/previews/530/530486_2532248-hq.mp3",
    "https://cdn.freesound.org:8443/previews/530/530486_2532248-hq.mp3",
    "https://cdn.freesound.org/previews/530/530486_2532248-hq.mp3?token=private",
    "file:///C:/private.mp3",
])
def test_untrusted_index_audio_url_is_rejected_before_audio_download(mock_http, url):
    calls = mock_http(lambda request: httpx.Response(200, json=raw_sound(url=url)))
    with pytest.raises(providers.ProviderError, match="公开试听地址"):
        ov.download_openverse({"source_id": ID})
    assert len(calls) == 1 and calls[0].url.host == "api.openverse.org"


@pytest.mark.parametrize("target", ["http://127.0.0.1/private", "https://evil.example/audio.mp3", "https://freesound.org/apiv2/sounds/530486/download/"])
def test_every_audio_redirect_is_validated_and_cannot_reach_authenticated_original(mock_http, target):
    def handler(request):
        if request.url.host == "api.openverse.org":
            return httpx.Response(200, json=raw_sound())
        return httpx.Response(302, headers={"location": target})

    calls = mock_http(handler)
    with pytest.raises(providers.ProviderError, match="公开试听地址"):
        ov.download_openverse({"source_id": ID})
    assert len(calls) == 2


def test_permitted_public_redirect_is_followed_without_credentials(mock_http):
    target = "https://freesound.org/data/previews/530/530486_2532248-hq.mp3"

    def handler(request):
        if request.url.host == "api.openverse.org":
            return httpx.Response(200, json=raw_sound())
        if str(request.url) == PREVIEW:
            return httpx.Response(302, headers={"location": target})
        assert str(request.url) == target
        return httpx.Response(200, content=AUDIO, headers={"content-type": "audio/mpeg"})

    calls = mock_http(handler)
    assert ov.download_openverse({"source_id": ID}) == (AUDIO, "mp3")
    assert len(calls) == 3


def test_invalid_id_never_makes_a_network_request(mock_http):
    calls = mock_http(lambda request: pytest.fail("must not request arbitrary path"))
    with pytest.raises(providers.ProviderError, match="ID"):
        ov.openverse_detail("../../settings")
    assert not calls


def test_other_sources_are_not_silently_reported_as_supported_sound_library(mock_http):
    mock_http(lambda request: httpx.Response(200, json=page([raw_sound(source="jamendo", category="music")], count=1, pages=1)))
    result = ov.search_openverse(expand_query("impact"))
    assert result["items"] == [] and "隐藏" in result["warning"]


def test_rate_limit_asks_to_wait_without_requiring_key_and_does_not_retry(mock_http):
    calls = mock_http(lambda request: httpx.Response(429, json={"detail": "secret should never be echoed"}))
    with pytest.raises(providers.ProviderError, match="稍等") as error:
        ov.search_openverse(expand_query("impact"))
    assert error.value.status_code == 429
    assert "密钥" not in str(error.value) and "Key" not in str(error.value)
    assert "secret" not in str(error.value)
    assert len(calls) == 1


def test_network_error_is_actionable_without_raw_exception(mock_http):
    def handler(request):
        raise httpx.ReadTimeout("private proxy info", request=request)

    mock_http(handler)
    with pytest.raises(providers.ProviderError, match="超时") as error:
        ov.search_openverse(expand_query("impact"))
    assert "private" not in str(error.value)


def test_public_audio_size_limit_is_enforced(mock_http, monkeypatch):
    monkeypatch.setattr(providers, "MAX_AUDIO_BYTES", 100)

    def handler(request):
        if request.url.host == "api.openverse.org":
            return httpx.Response(200, json=raw_sound())
        return httpx.Response(200, stream=httpx.ByteStream(b"x" * 101), headers={"content-type": "audio/mpeg"})

    mock_http(handler)
    with pytest.raises(providers.ProviderError, match="50 MB"):
        ov.download_openverse({"source_id": ID})
