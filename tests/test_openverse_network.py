"""DNS failure recovery remains scoped to anonymous Openverse GET requests."""
import ssl

import httpx
import pytest
import urllib3

from server import openverse_network as network, providers


@pytest.fixture(autouse=True)
def reset_route(monkeypatch):
    monkeypatch.setattr(network, "_ACTIVE", None)
    monkeypatch.setattr(network, "getproxies", lambda: {})
    monkeypatch.setattr(network, "proxy_bypass", lambda host: False)
    yield
    network._ACTIVE = None


def connection_error():
    return providers._network_error(httpx.ConnectError("private diagnostic"), network.PROVIDER)


def test_bad_dns_recovers_then_reuses_successful_route_until_expiry(monkeypatch):
    requests, resolved = [], []
    now = [100.0]
    monkeypatch.setattr(network.time, "monotonic", lambda: now[0])

    def request(method, url, provider, **kwargs):
        requests.append((url, kwargs))
        if url.startswith(network.API):
            raise connection_error()
        assert kwargs["params"] == {"name": network.HOST, "type": "A"}
        assert "Authorization" not in kwargs["headers"]
        return {"Status": 0, "Answer": [
            {"name": network.HOST, "type": 1, "data": "1.1.1.1", "TTL": 30},
        ]}

    def fallback(address, url, params):
        resolved.append((address, url, params))
        return {"results": [], "query": params["q"]}

    monkeypatch.setattr(providers, "_json_request", request)
    monkeypatch.setattr(network, "_resolved_json", fallback)
    assert network.get_json(params={"q": "bomb"})["query"] == "bomb"
    assert network.get_json(params={"q": "metal impact"})["query"] == "metal impact"
    assert len(requests) == 2 and len(resolved) == 2
    assert network._ACTIVE == ("1.1.1.1", 130)
    now[0] = 131
    network.get_json(params={"q": "sword"})
    assert len(requests) == 4  # Expired IP is discovered again, never hardcoded.


@pytest.mark.parametrize("error", [
    providers.ProviderError("no access", 401), providers.ProviderError("denied", 403),
    providers.ProviderError("rate limited", 429), providers.ProviderError("upstream", 502),
    providers._network_error(httpx.ReadTimeout("private"), network.PROVIDER),
    providers._network_error(httpx.ProxyError("private"), network.PROVIDER),
])
def test_http_errors_read_timeouts_and_proxy_rejection_do_not_trigger_dns(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(providers, "_json_request", fail)
    monkeypatch.setattr(network, "_addresses", lambda: pytest.fail("must not retry"))
    with pytest.raises(providers.ProviderError) as caught:
        network.get_json()
    assert caught.value is error


def test_unusable_dns_answers_cannot_reach_private_unrelated_or_multicast_hosts(monkeypatch):
    calls = []
    answers = [
        {"name": network.HOST, "type": 1, "data": ip, "TTL": 999}
        for ip in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "224.0.0.1", "0.0.0.0", "::1", "bad"]
    ] + [
        {"name": "unrelated.example", "type": 1, "data": "1.1.1.1", "TTL": 90},
        {"name": network.HOST, "type": 1, "data": "1.1.1.1", "TTL": "NaN"},
        {"name": network.HOST, "type": 1, "data": "1.1.1.1", "TTL": -10},
        {"name": network.HOST, "type": 5, "data": "1.1.1.1", "TTL": 90},
    ]
    def request(method, url, *args, **kwargs):
        calls.append(url)
        return {"Status": 0, "Answer": answers}
    monkeypatch.setattr(providers, "_json_request", request)
    assert network._addresses() == []
    assert calls == list(network.RESOLVERS)


def test_second_resolver_ttl_limit_zero_ttl_and_duplicate_addresses(monkeypatch):
    monkeypatch.setattr(network.time, "monotonic", lambda: 10)
    def request(method, url, *args, **kwargs):
        if url == network.RESOLVERS[0]:
            raise providers.ProviderError("timeout", 504)
        return {"Status": 0, "Answer": [
            {"name": network.HOST + ".", "type": 1, "data": "1.1.1.1", "TTL": 0},
            {"name": network.HOST, "type": 1, "data": "1.1.1.1", "TTL": 900},
            {"name": network.HOST, "type": 1, "data": "8.8.8.8", "TTL": 900},
        ]}
    monkeypatch.setattr(providers, "_json_request", request)
    assert network._addresses() == [("1.1.1.1", 10), ("8.8.8.8", 310)]


def test_no_resolver_result_has_actionable_error_without_raw_details(monkeypatch):
    def fail(*args, **kwargs):
        raise connection_error()
    monkeypatch.setattr(providers, "_json_request", fail)
    with pytest.raises(providers.ProviderError, match="备用域名解析") as caught:
        network.get_json()
    assert "private" not in str(caught.value)
    assert "不是缺少 API Key" in str(caught.value)


def test_failed_cached_route_is_discarded_and_normal_connection_can_recover(monkeypatch):
    monkeypatch.setattr(network, "_ACTIVE", ("1.1.1.1", network.time.monotonic() + 100))
    def fail(*args):
        raise network._ConnectionFailure()
    monkeypatch.setattr(network, "_resolved_json", fail)
    monkeypatch.setattr(providers, "_json_request", lambda *args, **kwargs: {"results": []})
    assert network.get_json() == {"results": []}
    assert network._ACTIVE is None


def test_proxy_tunnel_keeps_sni_and_verification_and_separates_proxy_credentials(monkeypatch):
    captured = {}
    monkeypatch.setattr(network, "getproxies", lambda: {"https": "http://name:pass%3Aword@127.0.0.1:4780"})
    def manager(url, **kwargs):
        captured.update(url=url, **kwargs)
        return object()
    monkeypatch.setattr(network.urllib3, "ProxyManager", manager)
    network._manager()
    assert captured["url"] == "http://127.0.0.1:4780"
    assert captured["server_hostname"] == captured["assert_hostname"] == network.HOST
    assert captured["ssl_context"].verify_mode == ssl.CERT_REQUIRED
    assert captured["retries"] is False
    assert captured["proxy_headers"] == urllib3.make_headers(proxy_basic_auth="name:pass:word")


def test_no_proxy_is_evaluated_for_original_host_and_unsupported_proxy_never_goes_direct(monkeypatch):
    monkeypatch.setattr(network, "getproxies", lambda: {"all": "socks5://127.0.0.1:4781"})
    monkeypatch.setattr(network.urllib3, "PoolManager", lambda **kwargs: "direct")
    with pytest.raises(network._ConnectionFailure):
        network._manager()
    def bypass(host):
        assert host == network.HOST
        return True
    monkeypatch.setattr(network, "proxy_bypass", bypass)
    assert network._manager() == "direct"


@pytest.mark.parametrize("status,body,error", [
    (200, b'{"results": []}', None), (403, b"secret", "拒绝访问"),
    (429, b"secret", "频繁"), (302, b"secret", "跳转"),
    (200, b"[]", "格式"), (200, b"not json", "无效数据"),
    (200, b"x" * 101, "过大"),
])
def test_fallback_response_bounds_status_errors_and_cleanup(monkeypatch, status, body, error):
    calls = []
    class Response:
        def __init__(self):
            self.status = status
        def stream(self, size, decode_content):
            assert decode_content
            yield body
        def close(self):
            calls.append("close")
        def release_conn(self):
            calls.append("release")
    class Manager:
        def request(self, method, url, **kwargs):
            assert method == "GET"
            assert url == "https://1.1.1.1/v1/audio/?q=metal+impact"
            assert kwargs["headers"]["Host"] == network.HOST
            assert set(kwargs["headers"]) == {"Host", "Accept", "User-Agent"}
            assert kwargs["redirect"] is kwargs["retries"] is kwargs["preload_content"] is False
            return Response()
        def clear(self):
            calls.append("clear")
    monkeypatch.setattr(network, "_manager", Manager)
    monkeypatch.setattr(providers, "MAX_JSON_BYTES", 100)
    if error:
        with pytest.raises(providers.ProviderError, match=error):
            network._resolved_json("1.1.1.1", network.API, {"q": "metal impact"})
    else:
        assert network._resolved_json("1.1.1.1", network.API, {"q": "metal impact"}) == {"results": []}
    assert calls == ["close", "release", "clear"]


def test_first_address_connection_failure_tries_second_but_http_error_stops(monkeypatch):
    monkeypatch.setattr(network, "_addresses", lambda: [("1.1.1.1", 100), ("8.8.8.8", 100)])
    def fail(*args, **kwargs):
        raise connection_error()
    monkeypatch.setattr(providers, "_json_request", fail)
    calls = []
    def resolved(address, *args):
        calls.append(address)
        if address == "1.1.1.1":
            raise network._ConnectionFailure()
        return {"results": []}
    monkeypatch.setattr(network, "_resolved_json", resolved)
    assert network.get_json() == {"results": []}
    assert calls == ["1.1.1.1", "8.8.8.8"]
    network._ACTIVE = None
    calls.clear()
    def denied(address, *args):
        calls.append(address)
        raise providers.ProviderError("denied", 403)
    monkeypatch.setattr(network, "_resolved_json", denied)
    with pytest.raises(providers.ProviderError):
        network.get_json()
    assert calls == ["1.1.1.1"]


def test_tls_error_during_response_body_does_not_repeat_request_and_closes_connection(monkeypatch):
    calls = []
    class Response:
        status = 200
        def stream(self, *args, **kwargs):
            raise urllib3.exceptions.SSLError("private TLS record error")
        def close(self):
            calls.append("close")
        def release_conn(self):
            calls.append("release")
    class Manager:
        def request(self, *args, **kwargs):
            calls.append("request")
            return Response()
        def clear(self):
            calls.append("clear")
    monkeypatch.setattr(network, "_ACTIVE", ("1.1.1.1", network.time.monotonic() + 30))
    monkeypatch.setattr(network, "_manager", Manager)
    monkeypatch.setattr(providers, "_json_request", lambda *args, **kwargs: pytest.fail("must not retry"))
    with pytest.raises(providers.ProviderError, match="连接中断") as error:
        network.get_json()
    assert "private" not in str(error.value)
    assert calls == ["request", "close", "release", "clear"]
