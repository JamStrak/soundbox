"""Offline pack transport/security/state regressions; no personal library imports."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import stat
import threading
import types
import wave
import zipfile

import httpx
import pytest

from server import gamepacks as gp
from server.audio import base_sound
from server.search import local_match
from server.storage import Library


LICENSE = b"Kenney - Creative Commons Zero CC0 https://creativecommons.org/publicdomain/zero/1.0/"


def wav_bytes():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(44100)
        writer.writeframes(b"\x00\x10\x00\xf0" * 2205)
    return buffer.getvalue()


def make_zip(entries=None, license_bytes=LICENSE):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        if license_bytes is not None:
            archive.writestr("License.txt", license_bytes)
        for path, content in entries or [("Audio/click_001.wav", wav_bytes()), ("Audio/click_002.wav", wav_bytes())]:
            archive.writestr(path, content)
        archive.writestr("Preview.ogg", b"not-an-importable-audio")
        archive.writestr("Visit Kenney.url", "https://kenney.nl")
    return output.getvalue()


def pack_for(data):
    pack = deepcopy(gp.PACKS["interface"])
    pack.update({"count": 2, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                 "license_sha256": hashlib.sha256(LICENSE).hexdigest(), "files": [
        {"path": f"Audio/click_00{i}.wav", "sha256": hashlib.sha256(wav_bytes()).hexdigest()} for i in (1, 2)]})
    return pack


@pytest.fixture
def setup(tmp_path, monkeypatch):
    data = make_zip()
    pack = pack_for(data)
    library = Library(tmp_path / "library")
    monkeypatch.setattr(gp, "PACKS", {"interface": pack})
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET" and str(request.url) == pack["download_url"]
        assert "authorization" not in request.headers and "xi-api-key" not in request.headers
        return httpx.Response(200, content=data, headers={"content-type": "application/zip"})

    monkeypatch.setattr(gp.providers, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(gp, "_audio_check", lambda path: {"audio_check": {"version": 1, "status": "ready", "flags": []}})
    return library, pack, data, calls


def test_official_manifest_has_exact_counts_and_each_entry_is_labeled():
    manifest = json.loads(Path(gp.__file__).with_name("gamepacks_manifest.json").read_text(encoding="utf-8"))
    assert [(p["id"], p["count"]) for p in manifest["packs"]] == [("interface", 100), ("impact", 130), ("digital", 62)]
    identifiers = set()
    for pack in manifest["packs"]:
        assert len(pack["files"]) == pack["count"]
        assert pack["license"] == "CC0" and pack["author"] == "Kenney"
        assert len(pack["sha256"]) == len(pack["license_sha256"]) == 64
        gp._safe_url(pack["download_url"])
        for entry in pack["files"]:
            assert entry["path"].startswith("Audio/") and "Preview" not in entry["path"]
            assert gp._group(entry) in pack["groups"]
            identifier = gp._identifier(pack, entry)
            assert identifier not in identifiers
            identifiers.add(identifier)


def test_install_decodes_and_persists_verified_sources_without_preview(setup):
    library, pack, data, calls = setup
    events = []
    result = gp.install_pack("interface", library, lambda *args: events.append(args))
    assert len(result) == len(library.all()) == 2 and len(calls) == 1
    sound = result[0]
    assert sound["duration"] == pytest.approx(.1, abs=.002) and len(sound["waveform"]) == 96
    assert sound["source"] == sound["origin_source"] == "kenney"
    assert sound["license"] == "CC0" and sound["quality"] == "original"
    assert sound["pack_id"] == "interface" and sound["variant_group"] == "kenney-interface-click"
    assert sound["author"] == "Kenney" and sound["source_url"] == pack["source_url"]
    assert sound["download_url"] == pack["download_url"]
    assert "点击" in sound["name"] and "click" in sound["name"]
    assert sound["audio_check"]["status"] == "ready"
    info = library.file(sound["id"]).stat()
    assert sound["audio_check_stamp"] == f"{info.st_size}:{info.st_mtime_ns}"
    assert events[-1][:2] == (2, 2)
    assert any("下载" in e[2] for e in events) and any("解析" in e[2] for e in events)
    assert all("Preview" not in s["source_file"] for s in result)
    assert gp.list_packs(library)[0]["installed_count"] == 2
    assert not list(library.media.glob(".kenney-*"))


def test_reinstall_is_offline_and_keeps_user_fields_then_repairs_missing_file(setup):
    library, pack, data, calls = setup
    first = gp.install_pack("interface", library)
    identifier = first[0]["id"]
    folder = library.add_folder("我的常用")
    fields = {"name": "用户改名", "favorite": True, "tags": ["私人标签"], "note": "音量已试用", "folder_id": folder["id"]}
    library.update(identifier, fields)
    before = library.file(identifier)
    result = gp.install_pack("interface", library)
    assert len(calls) == 1 and library.file(identifier) == before
    assert all(result[0][key] == value for key, value in fields.items())
    before.unlink()
    assert gp.list_packs(library)[0]["installed_count"] == 1
    restored = gp.install_pack("interface", library)[0]
    assert len(calls) == 2 and library.file(identifier).is_file()
    assert all(restored[key] == value for key, value in fields.items())


def test_cancel_after_first_commit_keeps_valid_entry_and_resumes(setup):
    library, pack, data, calls = setup
    cancelled = threading.Event()

    def progress(done, total, message):
        if done == 1:
            cancelled.set()

    with pytest.raises(gp.PackCancelled, match="已取消"):
        gp.install_pack("interface", library, progress, cancelled.is_set)
    assert len(library.all()) == 1 and library.file(library.all()[0]["id"])
    assert len(list(library.media.iterdir())) == 1
    cancelled.clear()
    gp.install_pack("interface", library)
    assert len(library.all()) == 2


def test_cancel_during_decoding_has_no_partial_row_or_media(setup, monkeypatch):
    library, _, _, _ = setup
    cancelled = threading.Event()
    inspect = gp.inspect_audio

    def decode(path):
        result = inspect(path)
        cancelled.set()
        return result

    monkeypatch.setattr(gp, "inspect_audio", decode)
    with pytest.raises(gp.PackCancelled):
        gp.install_pack("interface", library, cancelled=cancelled.is_set)
    assert not library.all() and not list(library.media.iterdir())


def test_cancel_before_download_makes_no_request(setup):
    library, _, _, calls = setup
    with pytest.raises(gp.PackCancelled):
        gp.install_pack("interface", library, cancelled=lambda: True)
    assert not calls and not library.all()


@pytest.mark.parametrize("path", ["../escaped.wav", "/absolute.wav", "C:/outside.wav", "Audio/../../outside.wav", "Audio\\outside.wav", "Audio/../bad.wav", "Audio//bad.wav", "Audio/file. /x.wav"])
def test_archive_rejects_unsafe_paths_before_import(path):
    data = make_zip([(path, b"bad")])
    # ZipInfo normalizes Windows separators while writing. Restore the raw ZIP
    # member name to exercise a hostile archive, not the platform's clean writer.
    if "\\" in path:
        data = data.replace(path.replace("\\", "/").encode(), path.encode())
    with pytest.raises(ValueError, match="不安全"):
        gp._verified_files(data, pack_for(data), None)


def test_archive_rejects_symlinks_duplicate_paths_and_member_limits(monkeypatch):
    symlink = zipfile.ZipInfo("Audio/link.wav")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    data = make_zip([(symlink, "../outside")])
    with pytest.raises(ValueError, match="不安全"):
        gp._verified_files(data, pack_for(data), None)
    data = make_zip([("Audio/same.wav", b"one"), ("audio/SAME.wav", b"two")])
    with pytest.raises(ValueError, match="不安全"):
        gp._verified_files(data, pack_for(data), None)
    data = make_zip()
    monkeypatch.setattr(gp, "MAX_MEMBERS", 2)
    with pytest.raises(ValueError, match="数量"):
        gp._verified_files(data, pack_for(data), None)


@pytest.mark.parametrize("constant", ["MAX_MEMBER_BYTES", "MAX_UNPACKED_BYTES"])
def test_archive_rejects_expansion_budget(constant, monkeypatch):
    data = make_zip()
    monkeypatch.setattr(gp, constant, 100)
    with pytest.raises(ValueError, match="解压体积"):
        gp._verified_files(data, pack_for(data), None)


def test_archive_rejects_checksum_license_and_unexpected_audio():
    data = make_zip()
    pack = pack_for(data)
    with pytest.raises(ValueError, match="校验失败"):
        gp._verified_files(data + b"trailing", pack, None)
    for license_bytes in (None, b"Copyright commercial only", LICENSE + b" Changed license"):
        changed = make_zip(license_bytes=license_bytes)
        with pytest.raises(ValueError, match="许可"):
            gp._verified_files(changed, pack_for(changed), None)
    changed = make_zip([("Audio/click_001.wav", wav_bytes()), ("Audio/unexpected.wav", wav_bytes())])
    with pytest.raises(ValueError, match="目录"):
        gp._verified_files(changed, pack_for(changed), None)
    wrong_audio = make_zip([("Audio/click_001.wav", b"wrong"), ("Audio/click_002.wav", wav_bytes())])
    with pytest.raises(ValueError, match="音频校验"):
        gp._verified_files(wrong_audio, pack_for(wrong_audio), None)


@pytest.mark.parametrize("url", ["http://kenney.nl/pack.zip", "https://evil.test/pack.zip", "https://127.0.0.1/pack.zip", "https://kenney.nl@evil.test/pack.zip", "https://kenney.nl/media/pages/assets/a.zip?secret=x"])
def test_only_fixed_official_https_downloads(url):
    with pytest.raises(ValueError, match="未经验证"):
        gp._safe_url(url)


@pytest.mark.parametrize("status", [302, 403, 429])
def test_download_does_not_follow_redirects_or_hide_http_errors(setup, monkeypatch, status):
    library, pack, _, _ = setup
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "http://127.0.0.1/private"})

    monkeypatch.setattr(gp.providers, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(gp.providers.ProviderError) as raised:
        gp.install_pack("interface", library)
    assert len(requests) == 1 and "Key" not in str(raised.value)
    assert not library.all() and not list(library.media.iterdir())


def test_network_error_and_oversized_response_are_sanitized(setup, monkeypatch):
    library, pack, data, _ = setup

    def broken(request):
        raise httpx.ConnectError("SECRET connection diagnostic", request=request)

    monkeypatch.setattr(gp.providers, "_client", lambda: httpx.Client(transport=httpx.MockTransport(broken)))
    with pytest.raises(gp.providers.ProviderError, match="Kenney") as raised:
        gp.install_pack("interface", library)
    assert "SECRET" not in str(raised.value)
    monkeypatch.setattr(gp.providers, "_client", lambda: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=data + b"extra"))))
    with pytest.raises(ValueError, match="大小"):
        gp.install_pack("interface", library)
    assert not library.all()


def test_decode_and_database_failure_never_leave_broken_entries(setup, monkeypatch):
    library, _, _, _ = setup
    with monkeypatch.context() as patch:
        patch.setattr(gp, "inspect_audio", lambda path: (_ for _ in ()).throw(ValueError("音频无法读取")))
        with pytest.raises(ValueError, match="无法读取"):
            gp.install_pack("interface", library)
    assert not library.all() and not list(library.media.iterdir())
    with monkeypatch.context() as patch:
        patch.setattr(library, "put", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(ValueError, match="磁盘"):
            gp.install_pack("interface", library)
    assert not library.all() and not list(library.media.iterdir())


def test_quality_is_optional_without_fabricating_ready_status(tmp_path, monkeypatch):
    module = types.SimpleNamespace(analyze_audio=lambda path: (_ for _ in ()).throw(ValueError("budget exceeded")))
    monkeypatch.setattr(gp.importlib, "import_module", lambda *args: module)
    result = gp._audio_check(tmp_path / "sound.wav")
    assert "audio_check" not in result and "待完成" in result["audio_check_error"]
    monkeypatch.setattr(gp.importlib, "import_module", lambda *args: (_ for _ in ()).throw(ModuleNotFoundError(name="server.quality")))
    assert "待完成" in gp._audio_check(tmp_path / "sound.wav")["audio_check_error"]


def test_true_per_sound_tags_match_both_languages_without_pack_keyword_pollution():
    manifest = json.loads(Path(gp.__file__).with_name("gamepacks_manifest.json").read_text(encoding="utf-8"))
    sounds = []
    for pack in manifest["packs"]:
        for entry in pack["files"]:
            sounds.append(gp._metadata(pack, entry, {"duration": 1, "waveform": [.2]}))
    metal = next(s for s in sounds if "impactMetal_heavy_000" in s["source_file"])
    click = next(s for s in sounds if s["source_file"] == "Audio/click_001.ogg")
    laser = next(s for s in sounds if s["source_file"] == "Audio/laser1.ogg")
    footsteps = next(s for s in sounds if s["source_file"] == "Audio/footstep_wood_000.ogg")
    confirm = next(s for s in sounds if s["source_file"] == "Audio/confirmation_001.ogg")
    def matches(query, sound):
        return local_match(query, sound["name"] + " " + " ".join(sound["tags"]))
    assert matches("金属撞击", metal) and matches("metal impact", metal)
    assert matches("界面点击", click) and matches("ui click", click)
    assert matches("激光", laser) and matches("laser", laser)
    assert matches("木地脚步", footsteps) and matches("wood footsteps", footsteps)
    assert matches("界面确认", confirm) and matches("UI confirm", confirm)
    assert not matches("玻璃", metal) and not matches("脚步", metal)
    assert not matches("激光", click) and not matches("强化", laser)
    assert not any(matches("雨声", sound) or matches("rain", sound) for sound in sounds)


def test_unknown_pack_and_existing_non_pack_id_are_never_overwritten(setup):
    library, pack, _, calls = setup
    with pytest.raises(ValueError, match="未知"):
        gp.install_pack("../private", library)
    sound = base_sound(gp._identifier(pack, pack["files"][0]), "Existing", "local", "wav")
    library.put(sound)
    with pytest.raises(ValueError, match="冲突"):
        gp.install_pack("interface", library)
    assert library.get(sound["id"])["source"] == "local" and not calls


def test_cancel_while_downloading_closes_stream_and_creates_no_files(setup, monkeypatch):
    library, pack, data, _ = setup
    cancelled = threading.Event()
    closed = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield data

        def close(self):
            closed.append(True)

    monkeypatch.setattr(gp.providers, "_client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, stream=Stream()))))

    def progress(done, total, message):
        if "字节" in message:
            cancelled.set()

    with pytest.raises(gp.PackCancelled):
        gp.install_pack("interface", library, progress, cancelled.is_set)
    assert closed and not library.all() and not list(library.media.iterdir())


def test_stream_size_is_checked_even_if_content_length_lies(setup, monkeypatch):
    library, pack, data, _ = setup
    monkeypatch.setattr(gp.providers, "_client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=data + b"extra", headers={"Content-Length": str(len(data))}))))
    with pytest.raises(ValueError, match="超过"):
        gp.install_pack("interface", library)
    assert not library.all()


def test_corrupted_zip_compression_returns_readable_error():
    data = bytearray(make_zip())
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entry = archive.getinfo("License.txt")
        compressed_offset = entry.header_offset + 30 + len(entry.filename.encode()) + len(entry.extra)
    data[compressed_offset] = 0xff
    with pytest.raises(ValueError, match="无法安全读取"):
        gp._verified_files(bytes(data), pack_for(data), None)


def test_second_install_is_rejected_while_first_decoder_is_running(setup, monkeypatch):
    library, _, _, _ = setup
    entered = threading.Event()
    release = threading.Event()
    errors = []
    original_inspect = gp.inspect_audio

    def inspect(path):
        entered.set()
        assert release.wait(5)
        return original_inspect(path)

    def install():
        try:
            gp.install_pack("interface", library)
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(gp, "inspect_audio", inspect)
    thread = threading.Thread(target=install)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(gp.providers.ProviderError) as raised:
            gp.install_pack("interface", library)
        assert raised.value.status_code == 409
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not errors and len(library.all()) == 2
