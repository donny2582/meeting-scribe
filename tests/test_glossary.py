"""詞庫讀取、STT 詞彙、誤寫校正（SPEC 第 8 章）。"""

from pathlib import Path

from meeting_scribe.config import assets_dir
from meeting_scribe.postprocess.glossary import (
    GlossaryEntry,
    load_glossaries,
    load_glossary,
    stt_phrases,
)
from meeting_scribe.postprocess.semantic import glossary_fix


def test_seed_glossary_loads() -> None:
    entries = load_glossary(assets_dir() / "glossary" / "seed_道安業務.csv")
    terms = [e.term for e in entries]
    assert "路政及道安司" in terms
    assert "LiDAR" in terms
    assert len(entries) >= 40


def test_phrases_priority_and_limit() -> None:
    entries = [
        GlossaryEntry("容錯道路工程", [], "術語"),
        GlossaryEntry("公路局", [], "機關"),
        GlossaryEntry("吳東凌", [], "人名"),
        GlossaryEntry("警察廳", [], "機關", ja="警察庁 → 警察廳"),
    ]
    assert stt_phrases(entries, ["zh"], limit=2) == ["公路局", "吳東凌"]
    assert "警察庁" in stt_phrases(entries, ["zh", "ja"])
    assert "警察庁" not in stt_phrases(entries, ["zh", "en"])


def test_corrections_longest_first() -> None:
    """詞庫「常見誤寫」取代（AI 校正使用的 glossary_fix）：最長的誤寫優先，並記下取代了什麼。"""
    entries = [
        GlossaryEntry("路政及道安司", ["路政及道安四", "路正及道安司"]),
        GlossaryEntry("道安", ["到安"]),
    ]
    text, used = glossary_fix("請路正及道安司與路政及道安四協助到安業務", entries)
    assert text == "請路政及道安司與路政及道安司協助道安業務"
    assert used == ["路正及道安司→路政及道安司", "路政及道安四→路政及道安司", "到安→道安"]


def test_corrections_no_fuzzy() -> None:
    entries = [GlossaryEntry("公路局", ["工路局"])]
    assert glossary_fix("公路總局", entries) == ("公路總局", [])  # 不做模糊比對


def test_merge_multiple(tmp_path: Path) -> None:
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    header = "正確詞,常見誤寫,類別,英文對照,日文對照,韓文對照,備註\n"
    a.write_text(header + "公路局,工路局,機關,,,,\n", encoding="utf-8-sig")
    b.write_text(header + "公路局,公路據|公路居,機關,,,,\n", encoding="utf-8-sig")
    merged = load_glossaries([a, b])
    assert len(merged) == 1
    assert merged[0].misspellings == ["工路局", "公路據", "公路居"]
