"""Pinned-catalog search and transport checks; no personal library or live APIs."""
from copy import deepcopy
import hashlib
import io
from pathlib import Path
import stat
import threading
import os
import time
import wave
import zipfile

import httpx
import pytest

from server import catalog
from server.search import expand_query


def wav_bytes():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(44100)
        output.writeframes(b"\x01\x10\x01\xf0" * 2205)
    return buffer.getvalue()


def archive_bytes(extra=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Audio/click.wav", wav_bytes())
        archive.writestr("Audio/metal-hit.wav", wav_bytes())
        archive.writestr("License.txt", b"CC0 by Example author")
        for name, data in extra or []:
            if isinstance(name, str) and "\\" in name:
                # ZipInfo normalizes Windows separators on this host, whereas
                # an untrusted archive can still contain the raw backslash.
                raw = zipfile.ZipInfo("placeholder")
                raw.filename = raw.orig_filename = name
                name = raw
            archive.writestr(name, data)
    return output.getvalue()


def manifest_for(data):
    audio = wav_bytes()
    return {"version": 1, "packs": [{
        "id": "test-pack", "name": "开放测试包", "original_name": "Test pack", "description": "游戏音效",
        "author": "Example author", "license": "CC0", "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
        "source_url": "https://opengameart.org/content/example", "license_evidence_url": "https://opengameart.org/content/example",
        "download_url": "https://opengameart.org/sites/default/files/test.zip", "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(), "license_files": [{"path": "License.txt", "sha256": hashlib.sha256(b"CC0 by Example author").hexdigest()}],
        "files": [{"path": path, "name": name, "tags": tags, "duration": duration, "format": "wav", "bytes": len(audio),
                   "sha256": hashlib.sha256(audio).hexdigest(), "sample_rate": 44100, "channels": 1, "codec": "pcm_s16le"}
                  for path, name, tags, duration in [("Audio/click.wav", "界面按钮点击 · UI Click", ["button", "click", "ui"], .1),
                                                   ("Audio/metal-hit.wav", "金属撞击 · Metal Impact", ["metal", "impact"], .2)]]
    }]}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    data = archive_bytes()
    manifest = manifest_for(data)
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET" and str(request.url) == manifest["packs"][0]["download_url"]
        assert "authorization" not in request.headers and "xi-api-key" not in request.headers
        return httpx.Response(200, content=data)

    monkeypatch.setattr(catalog.providers, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False))
    instance = catalog.Catalog(tmp_path / "data", manifest)
    first, second = list(instance.entries)
    return instance, manifest, data, calls, first, second


def test_search_is_bilingual_precise_and_does_not_download_or_create_library(setup):
    instance, _, _, calls, first, second = setup
    assert instance.search(expand_query("金属撞击"))["items"][0]["id"] == second
    assert instance.search(expand_query("metal impacts"))["items"][0]["id"] == second
    assert instance.search(expand_query("button click"))["items"][0]["id"] == first
    assert instance.search(expand_query("metal click"))["total"] == 0
    assert instance.search(expand_query(""), duration_max=.15)["total"] == 1
    assert [s["id"] for s in instance.search(expand_query(""), sort="duration_desc")["items"]] == [second, first]
    assert not calls and not list(instance.cache.iterdir())
    assert not (instance.directory / "library.sqlite3").exists()
    sound = instance.detail(first)
    assert sound["source"] == "catalog" and sound["origin_source"] == "opengameart"
    assert not sound["saved"] and not sound["downloaded"] and sound["waveform"] == []
    assert sound["quality"] == "original" and sound["codec"] == "pcm_s16le"
    assert sound["preview_url"] == "/api/catalog/audio/" + first
    assert sound["license"] == "CC0" and "Example author" in sound["attribution"]
    assert instance.packs()[0]["count"] == 2


def test_full_catalog_pagination(setup):
    instance, _, _, _, _, _ = setup
    assert instance.search(expand_query(""), page=2)["items"] == []
    assert not instance.search(expand_query(""), page=2)["has_more"]
    with pytest.raises(ValueError, match="不存在"):
        instance.search(expand_query(""), pack_id="missing")


def test_first_listen_extracts_only_requested_audio_and_reuses_verified_archive(setup):
    instance, _, data, calls, first, second = setup
    counted = []
    path = instance.audio_file(first, on_bytes=counted.append)
    assert path.read_bytes() == wav_bytes() and len(calls) == 1 and sum(counted) == len(data)
    assert len(list(instance.cache.glob("*.wav"))) == 1
    assert instance.audio_file(first) == path and len(calls) == 1
    assert instance.audio_file(second).read_bytes() == wav_bytes() and len(calls) == 1
    assert len(list(instance.cache.glob("*.wav"))) == 2
    assert not list(instance.cache.glob(".*.tmp"))
    assert instance.download(first) == (wav_bytes(), "wav")


def test_same_pack_parallel_listens_share_single_download(setup):
    instance, _, _, calls, first, second = setup
    results, errors = [], []

    def listen(identifier):
        try:
            results.append(instance.audio_file(identifier))
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=listen, args=(sound_id,)) for sound_id in [first, second, first]]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors and len(results) == 3 and len(calls) == 1


def test_checks_cannot_implicitly_fetch_whole_pack_and_budget_prevents_io(setup):
    instance, _, data, calls, first, _ = setup
    with pytest.raises(catalog.providers.ProviderError, match="请先试听"):
        instance.audio_file(first, allow_download=False)
    with pytest.raises(ValueError, match="预算"):
        instance.audio_file(first, max_download_bytes=len(data) - 1)
    assert not calls
    instance.audio_file(first)
    assert instance.audio_file(first, max_download_bytes=1, allow_download=False).is_file()


def test_invalid_browser_ids_fail_before_any_io(setup):
    instance, _, _, calls, _, _ = setup
    for invalid in ["../secret", "catalog-test-pack-nope", "https://example.org/audio.wav", ""]:
        with pytest.raises(ValueError, match="编号不存在"):
            instance.audio_file(invalid)
    assert not calls


@pytest.mark.parametrize("url", ["http://opengameart.org/sites/default/files/test.zip", "https://127.0.0.1/sites/default/files/test.zip",
                                 "https://opengameart.org.evil.test/sites/default/files/test.zip", "https://opengameart.org/sites/default/files/test.zip?token=secret",
                                 "https://kenney.nl/media/pages/assets/test.zip#fragment", "https://opengameart.org:8443/sites/default/files/test.zip",
                                 "https://user:pass@opengameart.org/sites/default/files/test.zip", "https://opengameart.org/sites/default/files/../test.zip"])
def test_only_fixed_official_https_archives_are_accepted(url):
    with pytest.raises(ValueError, match="地址未经核实"):
        catalog._safe_url(url)


def test_redirect_is_rejected_without_following(setup, monkeypatch):
    instance, _, _, _, first, _ = setup
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
    monkeypatch.setattr(catalog.providers, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False))
    with pytest.raises(catalog.providers.ProviderError, match="跳转"):
        instance.audio_file(first)
    assert len(calls) == 1 and not list(instance.cache.iterdir())


def test_wrong_archive_hash_and_changed_length_do_not_publish_audio(setup, monkeypatch):
    instance, _, data, _, first, _ = setup
    for response in [httpx.Response(200, content=data[:-1] + b"!"), httpx.Response(200, content=data, headers={"content-length": "999"})]:
        monkeypatch.setattr(catalog.providers, "_client", lambda: httpx.Client(transport=httpx.MockTransport(lambda request: response)))
        with pytest.raises(ValueError):
            instance.audio_file(first)
        assert not list(instance.cache.iterdir())


@pytest.mark.parametrize("extra", [[("../escape.wav", b"bad")], [("Audio\\escape.wav", b"bad")], [("Audio/CLICK.wav", wav_bytes())]])
def test_unsafe_zip_members_never_escape_cache(tmp_path, monkeypatch, extra):
    data = archive_bytes(extra)
    instance = catalog.Catalog(tmp_path / "data", manifest_for(data))
    monkeypatch.setattr(catalog.providers, "_client", lambda: httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=data))))
    with pytest.raises(ValueError, match="不安全"):
        instance.audio_file(next(iter(instance.entries)))
    assert not list(instance.cache.glob("*.wav")) and not (tmp_path / "escape.wav").exists()
    assert not list(instance.cache.glob(".*.tmp"))


def test_symlink_zip_member_is_rejected(tmp_path, monkeypatch):
    link = zipfile.ZipInfo("Audio/link.wav")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    data = archive_bytes([(link, b"/private")])
    instance = catalog.Catalog(tmp_path / "data", manifest_for(data))
    monkeypatch.setattr(catalog.providers, "_client", lambda: httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=data))))
    with pytest.raises(ValueError, match="不安全"):
        instance.audio_file(next(iter(instance.entries)))


def test_member_hash_and_license_hash_are_verified(tmp_path, monkeypatch):
    data = archive_bytes()
    for field in ["audio", "license"]:
        manifest = manifest_for(data)
        if field == "audio":
            manifest["packs"][0]["files"][0]["sha256"] = "0" * 64
        else:
            manifest["packs"][0]["license_files"][0]["sha256"] = "0" * 64
        instance = catalog.Catalog(tmp_path / field, manifest)
        monkeypatch.setattr(catalog.providers, "_client", lambda: httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=data))))
        with pytest.raises(ValueError, match="校验失败|许可文件"):
            instance.audio_file(next(iter(instance.entries)))
        assert not list(instance.cache.glob("*.wav"))


def test_corrupt_cached_asset_is_recovered_from_archive_without_network(setup):
    instance, _, _, calls, first, _ = setup
    path = instance.audio_file(first)
    path.write_bytes(b"broken")
    assert instance.audio_file(first).read_bytes() == wav_bytes() and len(calls) == 1


def test_cancelled_download_cleans_partial_file(setup):
    instance, _, _, calls, first, _ = setup
    stopped = threading.Event()
    with pytest.raises(ValueError, match="已取消"):
        instance.audio_file(first, on_bytes=lambda count: stopped.set(), cancelled=stopped.is_set)
    assert len(calls) == 1 and not list(instance.cache.iterdir())


def test_cache_budget_does_not_remove_personal_media_or_active_audio(setup, monkeypatch):
    instance, _, _, _, first, second = setup
    first_path = instance.audio_file(first)
    personal = instance.directory / "media"
    personal.mkdir()
    user_file = personal / "keep.wav"
    user_file.write_bytes(b"personal")
    monkeypatch.setattr(catalog, "CACHE_BUDGET", first_path.stat().st_size + 1)
    with pytest.raises(catalog.providers.ProviderError, match="缓存暂时已满"):
        instance.audio_file(second)
    assert first_path.is_file() and user_file.read_bytes() == b"personal"


def test_pinned_manifest_rejects_non_cc0_and_unverified_metadata(tmp_path):
    manifest = manifest_for(archive_bytes())
    for mutate in [lambda p: p.update(license="free"), lambda p: p.update(license_evidence_url=""),
                   lambda p: p["files"][0].update(duration=float("nan")), lambda p: p["files"][0].update(bytes=catalog.MAX_MEMBER_BYTES + 1)]:
        broken = deepcopy(manifest)
        mutate(broken["packs"][0])
        with pytest.raises(ValueError):
            catalog.Catalog(tmp_path, broken)


def test_browser_companion_is_budgeted_leased_and_removed_with_its_source(setup, monkeypatch):
    instance, _, _, _, first, _ = setup
    original = instance.audio_file(first)
    companion = original.with_name(original.stem + ".listen.mp3")
    companion.write_bytes(b"browser conversion")
    assert catalog._CACHE_NAME.fullmatch(companion.name)
    monkeypatch.setattr(catalog, "CACHE_BUDGET", companion.stat().st_size - 1)
    with pytest.raises(catalog.providers.ProviderError):
        instance._prune()
    assert companion.is_file() and original.is_file()
    instance.leases.clear()
    old = time.time() - catalog.CACHE_LIFETIME - 5
    os.utime(original, (old, old))
    os.utime(companion, (old, old))
    instance._prune()
    assert not original.exists() and not companion.exists()
