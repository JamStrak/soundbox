"""Recover Openverse GETs from bad local DNS without changing system settings.

Only a failed connection to this fixed public API enables HTTPS DNS discovery.
The fallback keeps the original Host, TLS SNI and certificate identity, including
inside HTTP proxy tunnels. httpcore 1.0.9 ignores custom SNI in that tunnel path,
so urllib3 handles this narrowly scoped, credential-free fallback instead.
"""
from __future__ import annotations

import ipaddress
import json
import math
import threading
import time
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import getproxies, proxy_bypass

import httpx
import urllib3

from . import providers

HOST = "api.openverse.org"
API = f"https://{HOST}/v1/audio/"
PROVIDER = "Openverse 公开搜索"
RESOLVERS = ("https://cloudflare-dns.com/dns-query", "https://dns.google/resolve")
_ACTIVE: tuple[str, float] | None = None
_LOCK = threading.Lock()


class _ConnectionFailure(Exception):
    pass


def _addresses() -> list[tuple[str, float]]:
    for resolver in RESOLVERS:
        try:
            data = providers._json_request(
                "GET", resolver, "备用域名解析",
                params={"name": HOST, "type": "A"},
                headers={"Accept": "application/dns-json"},
                timeout=httpx.Timeout(5, connect=3),
            )
        except providers.ProviderError:
            continue
        if data.get("Status") != 0 or not isinstance(data.get("Answer"), list):
            continue
        result, seen = [], set()
        for answer in data["Answer"]:
            if not isinstance(answer, dict) or answer.get("type") != 1:
                continue
            if str(answer.get("name", "")).rstrip(".").lower() != HOST:
                continue
            try:
                address = ipaddress.IPv4Address(answer.get("data"))
                ttl = float(answer.get("TTL", 0))
            except (ValueError, TypeError, ipaddress.AddressValueError):
                continue
            if (not address.is_global or address.is_multicast or address.is_reserved
                    or not math.isfinite(ttl) or ttl < 0 or str(address) in seen):
                continue
            seen.add(str(address))
            result.append((str(address), time.monotonic() + min(ttl, 300)))
            if len(result) == 2:
                break
        if result:
            return result
    return []


def _manager():
    # Match the existing client's certificate store and environment overrides.
    options = dict(
        server_hostname=HOST, assert_hostname=HOST,
        ssl_context=httpx.create_ssl_context(),
        timeout=urllib3.Timeout(connect=6, read=15), retries=False,
    )
    proxies = getproxies()
    proxy = None if proxy_bypass(HOST) else proxies.get("https") or proxies.get("all")
    if not proxy:
        return urllib3.PoolManager(**options)
    try:
        parts = urlsplit(proxy if "://" in proxy else "http://" + proxy)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("unsupported proxy")
        # Proxy credentials belong only in CONNECT, never the upstream request.
        proxy_headers = None
        if parts.username is not None:
            auth = unquote(parts.username) + ":" + unquote(parts.password or "")
            proxy_headers = urllib3.make_headers(proxy_basic_auth=auth)
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        authority = host + (f":{parts.port}" if parts.port else "")
        clean_proxy = urlunsplit((parts.scheme, authority, "", "", ""))
        return urllib3.ProxyManager(clean_proxy, proxy_headers=proxy_headers, **options)
    except (ValueError, TypeError, UnicodeError):
        # Do not silently bypass a configured but unsupported/broken proxy.
        raise _ConnectionFailure() from None


def _resolved_json(address: str, url: str, params: dict | None) -> dict:
    target = httpx.URL(url, params=params).copy_with(host=address)
    manager, response = None, None
    try:
        manager = _manager()
        response = manager.request(
            "GET", str(target),
            headers={"Host": HOST, "Accept": "application/json", "User-Agent": "Soundbox-Local/0.1"},
            preload_content=False, redirect=False, retries=False,
        )
        providers._status_error(httpx.Response(response.status), PROVIDER)
        chunks = bytearray()
        for chunk in response.stream(65536, decode_content=True):
            chunks.extend(chunk)
            if len(chunks) > providers.MAX_JSON_BYTES:
                raise providers.ProviderError(f"{PROVIDER} 返回的数据过大，请缩小请求范围。", 502)
        try:
            data = json.loads(chunks)
        except (ValueError, UnicodeError):
            raise providers.ProviderError(f"{PROVIDER} 返回了无效数据，请稍后重试。", 502) from None
        if not isinstance(data, dict):
            raise providers.ProviderError(f"{PROVIDER} 返回的数据格式不正确。", 502)
        return data
    except (urllib3.exceptions.ConnectTimeoutError, urllib3.exceptions.NewConnectionError,
            urllib3.exceptions.SSLError, urllib3.exceptions.ProxyError):
        if response is not None:
            # urllib3 also uses SSLError for corrupted TLS records while reading.
            # An established response must never be mistaken for failed DNS.
            raise providers.ProviderError(f"{PROVIDER} 连接中断，请稍后重试。", 502) from None
        raise _ConnectionFailure() from None
    except urllib3.exceptions.TimeoutError:
        raise providers.ProviderError(f"连接 {PROVIDER} 超时，请稍后重试。", 504) from None
    except (urllib3.exceptions.HTTPError, OSError):
        raise providers.ProviderError(f"{PROVIDER} 连接中断，请稍后重试。", 502) from None
    finally:
        if response is not None:
            response.close()
            response.release_conn()
        if manager is not None:
            manager.clear()


def get_json(path: str = "", *, params: dict | None = None) -> dict:
    global _ACTIVE
    # Callers already validate sound UUIDs; also keep this transport restricted
    # to the literal API prefix and reject queries/credentials/alternate hosts.
    url = API + path
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname != HOST or parts.query or parts.fragment
            or ".." in path or "\\" in path or path.startswith("/")):
        raise providers.ProviderError("Openverse 请求地址不正确。")
    with _LOCK:
        active = _ACTIVE if _ACTIVE and _ACTIVE[1] > time.monotonic() else None
    if active:
        try:
            return _resolved_json(active[0], url, params)
        except _ConnectionFailure:
            with _LOCK:
                if _ACTIVE == active:
                    _ACTIVE = None
    try:
        return providers._json_request("GET", url, PROVIDER, params=params)
    except providers.ProviderError as error:
        if not error.connection_failed:
            raise
    for address, expires in _addresses():
        try:
            data = _resolved_json(address, url, params)
        except _ConnectionFailure:
            continue
        with _LOCK:
            _ACTIVE = (address, expires)
        return data
    raise providers.ProviderError(
        "无法连接 Openverse，已尝试系统连接和备用域名解析。请确认代理可访问 api.openverse.org，"
        "或切换代理节点后重试；这不是缺少 API Key。", 502,
    )
