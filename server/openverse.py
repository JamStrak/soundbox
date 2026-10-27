"""Anonymous Openverse audio search, initially limited to Freesound's public index.

Only the primary public preview URL is used. Freesound ``alt_files`` points to
OAuth downloads and is deliberately ignored. No credentials are accepted here.
Official API source documents duration in milliseconds, 20 anonymous results per
page, a 240-result depth, coarse length buckets, and relevance-only anonymous sort.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import html
import math
import re
import threading
import time
from urllib.parse import urljoin, urlsplit
from uuid import UUID

import httpx

from . import providers, openverse_network

ProviderError = providers.ProviderError
API = "https://api.openverse.org/v1/audio/"
PAGE_SIZE = 20
MAX_PAGES = 12
CACHE_SECONDS = 600
CACHE_KEYS = 128
_SEARCH_CACHE: OrderedDict = OrderedDict()
_DETAIL_CACHE: OrderedDict = OrderedDict()
_CACHE_LOCK = threading.RLock()
_SORTS = {"score", "duration_asc", "duration_desc", "created_asc", "created_desc", "name"}
_PUBLIC_HOSTS = {"cdn.freesound.org", "freesound.org", "www.freesound.org"}
_PUBLIC_PATH = re.compile(r"^/(?:data/)?previews/[0-9]+/[0-9]+_[0-9]+(?:-[a-z]+)?\.(mp3|ogg|wav)$", re.I)
_LICENSES = {"cc0", "by", "by-sa", "by-nd", "by-nc", "by-nc-sa", "by-nc-nd", "pdm"}


def _source_id(value: str) -> str:
    raw = str(value).removeprefix("ov-")
    try:
        identifier = str(UUID(raw))
    except (ValueError, AttributeError, TypeError):
        raise ProviderError("Openverse 音效 ID 不正确，请重新搜索后保存。") from None
    if raw.lower() != identifier:
        raise ProviderError("Openverse 音效 ID 格式不正确，请重新搜索。")
    return identifier


def _public_url(url: str) -> str:
    """Allow only known Freesound preview paths, never its authenticated API."""
    try:
        parts = urlsplit(str(url))
        allowed = (
            parts.scheme == "https" and parts.hostname in _PUBLIC_HOSTS
            and parts.port in (None, 443) and not parts.username and not parts.password
            and not parts.query and not parts.fragment and bool(_PUBLIC_PATH.fullmatch(parts.path))
        )
    except (ValueError, TypeError):
        allowed = False
    if not allowed:
        raise ProviderError("这条音效没有已验证的公开试听地址；请前往来源页查看，不能自动下载授权接口或其他地址。")
    return str(url)


def _public_error(error: ProviderError) -> ProviderError:
    if error.status_code == 429:
        return ProviderError("Openverse 公开搜索暂时达到访问频率限制，请稍等片刻再试，避免连续点击；已有本地素材仍可使用。", 429)
    if error.status_code in {401, 403}:
        return ProviderError("开放网站暂时拒绝这次匿名访问，请稍后重试或前往来源网站；无需填写 API Key。", error.status_code)
    if error.status_code == 404:
        return ProviderError("开放索引中的音效或页面已不存在，请重新搜索，或前往音效来源页查看。", 404)
    return error


def _request(path: str = "", **kwargs) -> dict:
    try:
        return openverse_network.get_json(path, **kwargs)
    except ProviderError as error:
        raise _public_error(error) from None


def _cached(cache: OrderedDict, key):
    entry = cache.get(key)
    if entry is None:
        return None
    expires, value = entry
    if expires <= time.monotonic():
        del cache[key]
        return None
    cache.move_to_end(key)
    return deepcopy(value)


def _remember(cache: OrderedDict, key, value, limit: int):
    cache[key] = (time.monotonic() + CACHE_SECONDS, deepcopy(value))
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def _plain(value) -> str:
    return html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))


def _license(raw: dict) -> tuple[str, str, str]:
    code = str(raw.get("license") or "").lower()
    version = str(raw.get("license_version") or "")
    if not re.fullmatch(r"[1-4]\.0|2\.5", version):
        version = ""
    url = str(raw.get("license_url") or "")
    try:
        parsed = urlsplit(url)
        valid_url = parsed.scheme in {"http", "https"} and parsed.hostname in {"creativecommons.org", "www.creativecommons.org"} and not parsed.username and not parsed.password and parsed.port in (None, 80, 443)
    except ValueError:
        valid_url = False
    url = "https://creativecommons.org" + parsed.path if valid_url else ""
    if code not in _LICENSES:
        return "许可待核对", url, code
    if code == "cc0":
        return "CC0", url or "https://creativecommons.org/publicdomain/zero/1.0/", code
    if code == "pdm":
        return "Public Domain Mark", url or "https://creativecommons.org/publicdomain/mark/1.0/", code
    label = "CC " + code.upper() + (" " + version if version else "")
    if not url and version:
        url = f"https://creativecommons.org/licenses/{code}/{version}/"
    return label, url, code


def _sound(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ProviderError("Openverse 返回的音效数据格式不正确。", 502)
    identifier = _source_id(raw.get("id", ""))
    if raw.get("source") != "freesound":
        raise ProviderError("当前开放音效搜索仅接入 Freesound 的公开索引。")
    # Never fall back to alt_files: those can be authenticated originals.
    preview = _public_url(str(raw.get("url") or ""))
    extension = urlsplit(preview).path.rsplit(".", 1)[-1].lower()
    try:
        duration_ms = float(raw.get("duration") or 0)
    except (ValueError, TypeError):
        duration_ms = 0
    if not math.isfinite(duration_ms) or duration_ms < 0:
        duration_ms = 0
    tags = raw.get("tags") or []
    tags = [str(item.get("name") or "") for item in tags if isinstance(item, dict)] if isinstance(tags, list) else []
    license_name, license_url, license_code = _license(raw)
    landing_url = str(raw.get("foreign_landing_url") or "")
    try:
        landing_parts = urlsplit(landing_url)
        if landing_parts.scheme not in {"http", "https"} or landing_parts.hostname not in {"freesound.org", "www.freesound.org"} or landing_parts.username or landing_parts.password:
            landing_url = ""
        else:
            landing_url = "https://freesound.org" + landing_parts.path
    except ValueError:
        landing_url = ""
    attribution = _plain(raw.get("attribution"))
    metadata = raw.get("meta_data") if isinstance(raw.get("meta_data"), dict) else {}
    return {
        "id": "ov-" + identifier, "source_id": identifier, "source": "openverse", "origin_source": "freesound",
        "name": _plain(raw.get("title")) or "未命名音效", "tags": list(dict.fromkeys(tag for tag in tags if tag)),
        "description": _plain(raw.get("description") or metadata.get("description")) or "由 Openverse 索引的 Freesound 公开试听音效。",
        "duration": duration_ms / 1000, "duration_known": duration_ms > 0,
        "license": license_name, "license_url": license_url, "license_code": license_code,
        "author": _plain(raw.get("creator")), "source_url": landing_url or f"https://openverse.org/audio/{identifier}/",
        "index_url": f"https://openverse.org/audio/{identifier}/", "attribution": attribution,
        "preview_url": preview, "remote_preview_url": preview, "waveform": [],
        "favorite": False, "folder_id": None, "saved": False, "downloaded": False,
        "format": extension, "original_format": extension, "created_at": str(raw.get("indexed_on") or ""),
        "note": "", "quality": "preview", "download_label": "公开 " + extension.upper(),
    }


def _search_page(actual: str, page: int, license_filter: str) -> dict:
    key = (actual, page, license_filter)
    # Serialize uncached lookups to avoid duplicate anonymous requests for the
    # same page, and never expose the mutable internal cache to app/library code.
    with _CACHE_LOCK:
        cached = _cached(_SEARCH_CACHE, key)
        if cached is not None:
            return cached
        params = {"q": actual, "source": "freesound", "page": page, "page_size": PAGE_SIZE, "filter_dead": "true"}
        if license_filter == "cc0":
            params["license"] = "cc0"
        elif license_filter == "commercial":
            params["license"] = "cc0,by"
        raw = _request(params=params)
        if not isinstance(raw.get("results"), list):
            raise ProviderError("Openverse 返回的搜索列表格式不正确。", 502)
        try:
            total = max(0, int(raw["result_count"]))
            page_count = max(0, min(MAX_PAGES, int(raw["page_count"])))
        except (KeyError, ValueError, TypeError):
            raise ProviderError("Openverse 返回的分页信息不正确。", 502) from None
        items = []
        omitted = 0
        for item in raw["results"]:
            try:
                sound = _sound(item)
                if license_filter == "cc0" and sound["license_code"] != "cc0":
                    omitted += 1
                    continue
                if license_filter == "commercial" and sound["license_code"] not in {"cc0", "by"}:
                    omitted += 1
                    continue
            except ProviderError:
                omitted += 1
                continue
            items.append(sound)
            _remember(_DETAIL_CACHE, sound["source_id"], sound, CACHE_KEYS * PAGE_SIZE)
        result = {"items": items, "total": total, "page_count": page_count, "omitted": omitted}
        _remember(_SEARCH_CACHE, key, result, CACHE_KEYS)
        return deepcopy(result)


def search_openverse(query: dict, page: int = 1, license_filter: str = "all", duration_max: float = 0, sort: str = "score") -> dict:
    if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= MAX_PAGES:
        raise ProviderError("免密钥开放搜索最多查看前 12 页，请缩短或更换关键词继续查找。")
    if license_filter not in {"all", "commercial", "cc0"}:
        raise ProviderError("授权筛选参数不正确。")
    if sort not in _SORTS:
        raise ProviderError("开放搜索支持相关度、时长或本页索引时间排序，请更换排序方式。")
    if not isinstance(duration_max, (int, float)) or not math.isfinite(duration_max) or duration_max < 0:
        raise ProviderError("时长筛选必须为非负数。")
    actual = str(query.get("translated") or "").strip()
    if not actual or len(actual) > 200:
        raise ProviderError("请输入 1–200 字的音效关键词；描述较长时请保留最关键的几个词。")
    data = _search_page(actual, page, license_filter)
    items = data["items"]
    warnings = [str(query["warning"])] if query.get("warning") else []
    if data["omitted"]:
        warnings.append("本页已隐藏无法确认公开音频地址或不符合授权条件的条目；总数仍以开放索引为准。")
    if duration_max > 0:
        items = [item for item in items if item["duration_known"] and item["duration"] <= duration_max]
        warnings.append("精确时长仅筛选当前页，已排除时长未知的条目；总数未按时长重新统计，其他页可能还有符合条件的音效。")
    if sort != "score":
        field = "duration" if sort.startswith("duration") else "name" if sort == "name" else "created_at"
        items.sort(key=lambda item: item[field], reverse=sort.endswith("desc"))
        label = "索引时间" if sort.startswith("created") else "时长" if sort.startswith("duration") else "名称"
        warnings.append(f"免密钥搜索按相关度获取结果，当前仅按本页{label}排序。")
    result = {
        "items": items, "total": data["total"], "page": page,
        "has_more": page < data["page_count"], "query": deepcopy(query),
        "pagination_note": "Openverse 开放索引 · 当前来源 Freesound · 每页 20 条，最多查看前 240 条（12 页）；公开试听文件可直接保存。",
    }
    if warnings:
        result["warning"] = " ".join(warnings)
    return result


def openverse_detail(source_id: str) -> dict:
    identifier = _source_id(source_id)
    with _CACHE_LOCK:
        cached = _cached(_DETAIL_CACHE, identifier)
        if cached is not None:
            return cached
        raw = _request(identifier + "/")
        sound = _sound(raw)
        if sound["source_id"] != identifier:
            raise ProviderError("Openverse 返回的音效 ID 与请求不一致。", 502)
        _remember(_DETAIL_CACHE, identifier, sound, CACHE_KEYS * PAGE_SIZE)
        return deepcopy(sound)


def download_openverse(sound: dict, **read_options) -> tuple[bytes, str]:
    # Resolve by ID on the server even for saved items. Never trust URL fields in
    # a browser request, and refresh expired search metadata before downloading.
    identifier = _source_id(sound.get("source_id") or sound.get("id", ""))
    trusted = openverse_detail(identifier)
    current = _public_url(trusted["remote_preview_url"])
    extension = urlsplit(current).path.rsplit(".", 1)[-1].lower()
    try:
        with providers._client() as client:
            for _ in range(6):
                with client.stream("GET", current, headers={"Accept": "audio/*,application/octet-stream"}) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise ProviderError("公开音频跳转缺少地址，请前往来源页查看。", 502)
                        current = _public_url(urljoin(current, location))
                        extension = urlsplit(current).path.rsplit(".", 1)[-1].lower()
                        continue
                    try:
                        return providers._read_audio(response, "开放音频网站", **read_options), extension
                    except ProviderError as error:
                        raise _public_error(error) from None
        raise ProviderError("公开音频跳转次数过多，请稍后重试。", 502)
    except httpx.HTTPError as error:
        raise providers._network_error(error, "开放音频网站") from None
