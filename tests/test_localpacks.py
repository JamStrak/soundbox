"""User-owned ZIP indexing/lazy reading regressions, using isolated tiny packs."""
import sqlite3
import stat
import struct
import zipfile

import pytest

from server import localpacks as lp


def wav_bytes(bits=24, channels=2, rate=48000, encoding=1, seconds=.01, extensible=False):
    align = channels * (bits // 8)
    frames = max(1, round(rate * seconds))
    payload = b"\0" * frames * align
    fmt = struct.pack("<HHIIHH", 0xFFFE if extensible else encoding, channels, rate, rate * align, align, bits)
    if extensible:
        fmt += struct.pack("<HHI", 22, bits, 0) + struct.pack("<I", encoding) + bytes.fromhex("00001000800000aa00389b71")
    body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body


def make_pack(path, entries=None, license=True):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        if license:
            archive.writestr("License - GDC Game Audio.pdf", b"user-provided licence")
        for name, content in entries or [("Studio - Footsteps/Stone Footsteps.wav", wav_bytes())]:
            archive.writestr(name, content)
    return path


def test_header_only_index_supports_high_resolution_and_keeps_source(tmp_path):
    pack = make_pack(tmp_path / "Sonniss.com-GDC2026-GameAudioBundle1of5.zip", [
        ("Studio - Foley/Metal Impact.wav", wav_bytes()),
        ("Studio - Creatures/Monster Growl.wav", wav_bytes(bits=32, channels=1, rate=192000, encoding=3)),
        ("Studio - Surround/Crowd.wav", wav_bytes(channels=6, extensible=True)),
        ("do-not-open.exe", b"not audio"),
    ])
    original, stamp = pack.read_bytes(), pack.stat().st_mtime_ns
    packs = lp.LocalPacks(tmp_path / "data")
    result = packs.index_directory(str(tmp_path))
    assert result["count"] == 3 and result["archives"] == 1 and not result["skipped"]
    assert not list(packs.cache.iterdir())
    assert pack.read_bytes() == original and pack.stat().st_mtime_ns == stamp
    sound = result["items"][0]
    assert sound["license"] == "Sonniss GDC 项目使用许可" and sound["license_url"] == lp.SONNISS_LICENSE_URL
    assert sound["source_id"] == sound["id"] and sound["source"] == "localpack"
    assert not sound["saved"] and not sound["downloaded"] and sound["waveform"] == []
    assert (sound["bit_depth"], sound["sample_rate"], sound["channels"]) == (24, 48000, 2)
    assert result["items"][1]["codec"] == "pcm_f32le" and result["items"][2]["channels"] == 6
    assert len(lp.LocalPacks(packs.directory).all()) == 3
    assert packs.cached_file(sound["id"]) is None


def test_lazy_materialization_verifies_crc_and_repairs_bad_cache(tmp_path):
    audio = wav_bytes()
    pack = make_pack(tmp_path / "owned.zip", [("Foley/click.wav", audio)])
    packs = lp.LocalPacks(tmp_path / "data")
    identifier = packs.index_directory(str(pack))["items"][0]["id"]
    assert packs.detail(identifier)["license"] == "待确认"
    target = packs.audio_file(identifier)
    assert target.parent == packs.cache and target.read_bytes() == audio
    assert packs.cached_file(identifier) == target
    companion = target.with_name(target.stem + ".listen.mp3")
    companion.write_bytes(b"outdated compatibility preview")
    target.write_bytes(b"x" * len(audio))
    assert packs.cached_file(identifier) is None
    assert not companion.exists()
    assert packs.download(identifier) == (audio, "wav")
    assert not list(packs.cache.glob("*.part"))


def test_zip_paths_symlinks_executables_and_duplicate_members_are_not_read(tmp_path):
    pack = tmp_path / "bad.zip"
    linked = zipfile.ZipInfo("link.wav")
    linked.create_system = 3
    linked.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(pack, "w") as archive:
        for path in ("../outside.wav", "/absolute.wav", "C:/evil.wav", "back\\slash.wav", "CON.wav"):
            archive.writestr(path, wav_bytes())
        archive.writestr(linked, b"outside.wav")
        archive.writestr("Good/click.wav", wav_bytes())
        archive.writestr("Duplicate/click.wav", wav_bytes())
        archive.writestr("Duplicate/CLICK.wav", wav_bytes())
        archive.writestr("danger.exe", b"x")
    # On Windows ZipInfo normalizes backslashes when constructing archives;
    # change the equal-length on-disk member name to exercise hostile input.
    pack.write_bytes(pack.read_bytes().replace(b"back/slash.wav", b"back\\slash.wav"))
    packs = lp.LocalPacks(tmp_path / "data")
    result = packs.index_directory(str(pack))
    assert result["count"] == 1 and len(result["skipped"]) == 8
    assert packs.audio_file(result["items"][0]["id"]).is_relative_to(packs.cache)
    assert not (tmp_path / "outside.wav").exists()
    with pytest.raises(ValueError):
        packs.audio_file("../../outside")


def test_changed_or_missing_source_requires_reindex_even_when_cached(tmp_path):
    pack = make_pack(tmp_path / "owned.zip")
    packs = lp.LocalPacks(tmp_path / "data")
    identifier = packs.index_directory(str(pack))["items"][0]["id"]
    packs.audio_file(identifier)
    make_pack(pack, [("Studio - Footsteps/Stone Footsteps.wav", wav_bytes(seconds=.02))])
    with pytest.raises(ValueError, match="重新索引"):
        packs.audio_file(identifier)
    refreshed = packs.index_directory(str(pack))["items"][0]
    assert refreshed["id"] == identifier and refreshed["duration"] == .02
    pack.unlink()
    with pytest.raises(ValueError, match="重新索引"):
        packs.cached_file(identifier)


def test_cancelled_index_keeps_completed_metadata_for_caller(tmp_path):
    pack = make_pack(tmp_path / "owned.zip", [("a.wav", wav_bytes()), ("b.wav", wav_bytes())])
    packs = lp.LocalPacks(tmp_path / "data")
    stop = False

    def progress(completed, total, message):
        nonlocal stop
        if completed == 1:
            stop = True

    with pytest.raises(lp.LocalPackCancelled) as captured:
        packs.index_directory(str(pack), progress, lambda: stop)
    assert captured.value.summary["count"] == 1 and len(captured.value.items) == 1
    assert len(packs.all()) == 1 and not list(packs.cache.iterdir())


def test_duration_and_header_budget_limits_report_skipped(tmp_path, monkeypatch):
    pack = make_pack(tmp_path / "owned.zip", [
        ("long.wav", wav_bytes(bits=8, channels=1, rate=8000, seconds=601)),
        ("fake.wav", b"not wav"), ("good.wav", wav_bytes()),
    ])
    packs = lp.LocalPacks(tmp_path / "data")
    result = packs.index_directory(str(pack))
    assert result["count"] == 1 and len(result["skipped"]) == 2
    assert any("600" in item for item in result["skipped"])
    monkeypatch.setattr(lp, "MAX_HEADER_BYTES", 20)
    result = packs.index_directory(str(pack))
    assert result["count"] == 0 and any("文件头过大" in item for item in result["skipped"])


def test_cache_eviction_stays_within_budget_and_preserves_archives(tmp_path, monkeypatch):
    audio = wav_bytes()
    pack = make_pack(tmp_path / "owned.zip", [("a.wav", audio), ("b.wav", audio)])
    packs = lp.LocalPacks(tmp_path / "data")
    sounds = packs.index_directory(str(pack))["items"]
    monkeypatch.setattr(lp, "MAX_CACHE_BYTES", len(audio) + 1)
    monkeypatch.setattr(lp, "AUDIO_LEASE", 0)
    first = packs.audio_file(sounds[0]["id"])
    second = packs.audio_file(sounds[1]["id"])
    assert not first.exists() and second.read_bytes() == audio and pack.exists()
    assert sum(path.stat().st_size for path in packs.cache.iterdir()) <= lp.MAX_CACHE_BYTES


def test_registry_tampering_cannot_choose_member_or_cache_path(tmp_path):
    pack = make_pack(tmp_path / "owned.zip")
    packs = lp.LocalPacks(tmp_path / "data")
    identifier = packs.index_directory(str(pack))["items"][0]["id"]
    with sqlite3.connect(packs.path) as db:
        db.execute("UPDATE local_sounds SET member='../outside.wav' WHERE id=?", (identifier,))
    with pytest.raises(ValueError, match="索引记录无效"):
        packs.audio_file(identifier)
    assert not list(packs.cache.iterdir())


def test_lru_accounts_for_preview_companions_and_orphans(tmp_path, monkeypatch):
    audio = wav_bytes()
    pack = make_pack(tmp_path / "owned.zip", [("a.wav", audio), ("b.wav", audio)])
    packs = lp.LocalPacks(tmp_path / "data")
    sounds = packs.index_directory(str(pack))["items"]
    monkeypatch.setattr(lp, "AUDIO_LEASE", 0)
    first = packs.audio_file(sounds[0]["id"])
    companion = first.with_name(first.stem + ".listen.mp3")
    companion.write_bytes(b"mp3" * 100)
    monkeypatch.setattr(lp, "MAX_CACHE_BYTES", len(audio) + 301)
    second = packs.audio_file(sounds[1]["id"])
    assert second.exists() and not first.exists() and not companion.exists()
    # Orphan compatibility files are still owned cache entries and are budgeted.
    companion.write_bytes(b"mp3" * 100)
    monkeypatch.setattr(lp, "MAX_CACHE_BYTES", len(audio))
    with packs.lock:
        packs._prune(0)
    assert sum(p.stat().st_size for p in packs.cache.iterdir()) <= len(audio)


def test_leased_audio_and_compatible_preview_are_not_evicted(tmp_path, monkeypatch):
    audio = wav_bytes()
    pack = make_pack(tmp_path / "owned.zip", [("a.wav", audio), ("b.wav", audio)])
    packs = lp.LocalPacks(tmp_path / "data")
    sounds = packs.index_directory(str(pack))["items"]
    monkeypatch.setattr(lp, "MAX_BROWSER_PREVIEW_BYTES", 300)
    monkeypatch.setattr(lp, "MAX_CACHE_BYTES", len(audio) + 300)
    monkeypatch.setattr(lp, "AUDIO_LEASE", 0)
    with packs.lease_file(sounds[0]["id"]) as first:
        companion = first.with_name(first.stem + ".listen.mp3")
        companion.write_bytes(b"mp3" * 100)
        with pytest.raises(ValueError, match="缓存空间不足"):
            packs.audio_file(sounds[1]["id"])
        assert first.exists() and companion.exists()
    second = packs.audio_file(sounds[1]["id"])
    assert second.exists() and not first.exists() and not companion.exists()


def test_file_response_playback_lease_outlives_method_return(tmp_path, monkeypatch):
    audio = wav_bytes()
    pack = make_pack(tmp_path / "owned.zip", [("a.wav", audio), ("b.wav", audio)])
    packs = lp.LocalPacks(tmp_path / "data")
    sounds = packs.index_directory(str(pack))["items"]
    monkeypatch.setattr(lp, "MAX_CACHE_BYTES", len(audio) + 1)
    first = packs.audio_file(sounds[0]["id"])
    with pytest.raises(ValueError, match="缓存空间不足"):
        packs.audio_file(sounds[1]["id"])
    assert first.exists()
    monkeypatch.setattr(lp.time, "monotonic", lambda: 10 ** 20)
    second = packs.audio_file(sounds[1]["id"])
    assert second.exists() and not first.exists()


def test_pack_groups_use_actual_subpack_name_not_archive_volume(tmp_path):
    pack = make_pack(tmp_path / "owned.zip", [
        ("Studio A - Footsteps/a.wav", wav_bytes()),
        ("Studio B - Creatures/b.wav", wav_bytes()),
    ])
    sounds = lp.LocalPacks(tmp_path / "data").index_directory(str(pack))["items"]
    assert len({sound["pack_id"] for sound in sounds}) == 2
    assert sounds[0]["pack_name"] == "Studio A - Footsteps"
    assert sounds[1]["pack_name"] == "Studio B - Creatures"
    assert sounds[0]["archive_name"] == sounds[1]["archive_name"] == "owned.zip"
