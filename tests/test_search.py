import pytest

from server.search import expand_query, local_match


@pytest.mark.parametrize(("query", "terms"), [
    ("金属撞击", ["metal", "impact"]),
    ("金屬撞擊", ["metal", "impact"]),
    ("木頭腳步", ["wood", "footstep"]),
    ("刀剑挥砍", ["sword", "slash"]),
    ("按鈕確認", ["button", "confirmation"]),
    ("魔法火球爆炸", ["magic", "fireball", "explosion"]),
    ("森林鸟鸣", ["forest", "bird"]),
    ("森林鳥鳴", ["forest", "bird"]),
    ("dogs wolves arrows swords", ["dog", "wolf", "arrow", "sword"]),
    ("Footsteps WOODEN", ["footstep", "wood"]),
    ("magic 火球 impacts", ["magic", "fireball", "impact"]),
    ("玻璃 glass", ["glass"]),
    ("UI clicks", ["ui", "click"]),
    ("炮弹", ["cannon"]),
    ("炮彈", ["cannon"]),
    ("枪械换弹", ["gun", "reload"]),
    ("WPN GUN reload", ["weapon", "gun", "reload"]),
    ("AMBI FOOT", ["ambience", "footstep"]),
    ("Sound effects of a sword", ["sword"]),
])
def test_bilingual_vocabulary(query, terms):
    result = expand_query(query)
    assert result["original"] == query
    assert result["terms"] == terms
    assert result["translated"] == " ".join(terms)
    assert not result.get("warning")


def test_synonyms_are_not_added_as_mandatory_query_terms():
    assert expand_query("挥动")['translated'] == "whoosh"
    assert expand_query("打击 击打 impact hit strike")['terms'] == ["impact"]


def test_spoken_description_ignores_filler_and_warns_for_long_query():
    result = expand_query("我想要一把厚重的铁剑砍到盾牌上，声音短促，有金属震动")
    assert result["terms"] == ["heavy", "sword", "impact", "shield", "short", "metal", "vibration"]
    assert "关键词较多" in result["warning"]
    assert "词库未覆盖" not in result["warning"]


def test_unknown_chinese_is_retained_and_not_reported_as_translated():
    result = expand_query("古筝量子纠缠")
    assert result["translated"] == "古筝量子纠缠"
    assert "词库未覆盖" in result["warning"]
    assert "古筝量子纠缠" in result["warning"]


def test_partial_unknown_chinese_is_visible():
    result = expand_query("魔法泡芙")
    assert result["terms"] == ["magic", "泡芙"]
    assert "泡芙" in result["warning"]


@pytest.mark.parametrize(("query", "metadata", "expected"), [
    ("金属撞击", "Heavy steel hit", True),
    ("metal impacts", "金屬撞擊 斧头", True),
    ("木头脚步", "wooden footsteps on floor", True),
    ("金属脚步", "wooden footsteps on floor", False),
    ("wolves", "wolf howl", True),
    ("unknown", "unknown_thing.wav", True),
    ("cat", "notification", False),
    ("火球", "fireball sci-fi", True),
    ("我的自定义", "我的自定义素材", True),
    ("", "anything", True),
])
def test_local_bilingual_matching(query, metadata, expected):
    assert local_match(query, metadata) is expected


def test_only_filler_is_not_a_match_all_query():
    result = expand_query("音效")
    assert result["translated"] == "音效"
    assert "未识别" in result["warning"]
    assert local_match("音效", "WAV 123") is False
