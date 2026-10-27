"""Verified media inventory and real-catalog relevance regressions.

No network, source media, paid APIs or personal data are used by these checks.
"""
import importlib.util
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit

import pytest

from server.catalog import Catalog, _safe_url, identifier
from server.search import expand_query, local_match


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "server" / "catalog_manifest.json").read_text(encoding="utf-8"))


def load_builder():
    spec = importlib.util.spec_from_file_location("catalog_builder", ROOT / "tools" / "build_catalog_manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shipped_manifest_has_measured_unique_open_audio():
    assert MANIFEST["version"] == 1
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", MANIFEST["verified_at"])
    packs = MANIFEST["packs"]
    assert len(packs) >= 28
    assert len({pack["author"] for pack in packs}) >= 11
    entries = [(pack, entry) for pack in packs for entry in pack["files"]]
    assert len(entries) >= 2278
    assert len({entry["sha256"] for _, entry in entries}) == len(entries)
    assert len({identifier(pack, entry) for pack, entry in entries}) == len(entries)
    for pack in packs:
        assert pack["license"] == "CC0"
        assert pack["license_url"] == "https://creativecommons.org/publicdomain/zero/1.0/"
        assert pack["license_evidence_url"] == pack["source_url"]
        assert urlsplit(pack["source_url"]).hostname in {"kenney.nl", "opengameart.org"}
        assert _safe_url(pack["download_url"]) == pack["download_url"]
        assert re.fullmatch(r"[0-9a-f]{64}", pack["sha256"])
        assert re.fullmatch(r"[0-9a-f]{64}", pack["license_evidence_sha256"])
    for _, entry in entries:
        assert 0 < entry["bytes"] <= 50 * 1024 * 1024
        assert math.isfinite(entry["duration"]) and 0 < entry["duration"] <= 600
        assert entry["sample_rate"] > 0 and entry["channels"] > 0
        assert entry["codec"] not in {"", "unknown"}
        if entry["format"] == "wav":
            assert entry["codec"].startswith("pcm_")
        elif entry["format"] == "ogg":
            assert entry["codec"] == "vorbis"


@pytest.fixture
def catalog(tmp_path):
    return Catalog(tmp_path / "isolated-library", MANIFEST)


def test_misc_does_not_inherit_other_items_spells_from_pack_name(catalog):
    misc = [entry for pack, entry in catalog.entries.values()
            if pack["id"] == "oga-rpg" and re.fullmatch(r"misc_\d+\.ogg", entry["path"])]
    assert len(misc) == 3
    for entry in misc:
        assert entry["name"].startswith("杂项")
        assert not local_match("magic", " ".join([entry["name"], *entry["tags"]]))
    result = catalog.search(expand_query("magic"), pack_id="oga-rpg")
    assert result["total"] > 0
    assert all(not re.fullmatch(r"misc_\d+\.ogg", catalog.entries[item["id"]][1]["path"])
               for item in result["items"])
    assert not list(catalog.cache.iterdir())


def test_unknown_labels_have_neutral_fallback_and_air_is_specific():
    builder = load_builder()
    name, tags = builder.labels("unknown_01.ogg", ["rpg", "foley"], "法术与物品大合集")
    assert "法术" not in name and "物品" not in name
    name, tags = builder.labels("xyz_01.ogg", ["rpg", "foley"], "法术与物品大合集")
    assert name.startswith("杂项音效")
    name, tags = builder.labels("sfx100v2_air_01.ogg", ["foley"], "环境与机械大合集")
    assert name.startswith("空气") and "空气" in tags
    assert "机械" not in name


def test_casino_card_fan_means_spreading_cards_not_ventilation():
    builder = load_builder()
    name, tags = builder.labels("Audio/card-fan1.ogg", ["桌游", "casino"], "纸牌与骰子")
    assert "纸牌" in name and "扇牌" in name
    assert "风扇" not in name and "风扇" not in tags
    casino = next(pack for pack in MANIFEST["packs"] if pack["id"] == "kenney-casino")
    cards = [entry for entry in casino["files"] if "card-fan" in entry["path"]]
    assert cards
    assert all("扇牌" in entry["name"] and "风扇" not in entry["name"] for entry in cards)
    name, tags = builder.labels("fan_01.wav", ["foley"], "机械")
    assert "风扇" in name and "风扇" in tags
