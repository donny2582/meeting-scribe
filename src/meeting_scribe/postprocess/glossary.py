"""自訂詞庫（SPEC 第 8 章）：讀取、STT 提示詞彙、誤寫校正。"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

COLUMNS = ["正確詞", "常見誤寫", "類別", "英文對照", "日文對照", "韓文對照", "備註"]
PRIORITY_CATEGORIES = ("機關", "人名")


@dataclass
class GlossaryEntry:
    """詞庫中的一筆資料。"""

    term: str
    misspellings: list[str]
    category: str = ""
    en: str = ""
    ja: str = ""
    ko: str = ""
    note: str = ""


def _split_pair(value: str) -> str:
    """取出「原文 → 譯名」中的原文。"""
    for arrow in ("→", "->"):
        if arrow in value:
            return value.split(arrow, 1)[0].strip()
    return value.strip()


def load_glossary(path: Path) -> list[GlossaryEntry]:
    """讀取詞庫 CSV（UTF-8，可含 BOM）。"""
    entries: list[GlossaryEntry] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            term = (row.get("正確詞") or "").strip()
            if not term:
                continue
            miss = [m.strip() for m in (row.get("常見誤寫") or "").replace("|", "｜").split("｜") if m.strip()]
            entries.append(
                GlossaryEntry(
                    term=term,
                    misspellings=miss,
                    category=(row.get("類別") or "").strip(),
                    en=(row.get("英文對照") or "").strip(),
                    ja=(row.get("日文對照") or "").strip(),
                    ko=(row.get("韓文對照") or "").strip(),
                    note=(row.get("備註") or "").strip(),
                )
            )
    return entries


def load_glossaries(paths: Iterable[Path]) -> list[GlossaryEntry]:
    """讀取多個詞庫檔並合併（同一正確詞以先出現者為準，誤寫合併）。"""
    merged: dict[str, GlossaryEntry] = {}
    for path in paths:
        for entry in load_glossary(path):
            if entry.term in merged:
                existing = merged[entry.term]
                existing.misspellings += [m for m in entry.misspellings if m not in existing.misspellings]
            else:
                merged[entry.term] = entry
    return list(merged.values())


def stt_phrases(entries: list[GlossaryEntry], languages: Iterable[str], limit: int | None = None) -> list[str]:
    """產生送給 STT 引擎的詞彙清單；超過上限時優先保留機關、人名。"""
    langs = set(languages)
    prioritized = sorted(entries, key=lambda e: 0 if e.category in PRIORITY_CATEGORIES else 1)
    phrases: list[str] = []
    for e in prioritized:
        candidates = [e.term]
        if "ja" in langs and e.ja:
            candidates.append(_split_pair(e.ja))
        if "ko" in langs and e.ko:
            candidates.append(_split_pair(e.ko))
        for p in candidates:
            if p and p not in phrases:
                phrases.append(p)
    return phrases[:limit] if limit else phrases


# 「常見誤寫 → 正確詞」的取代在 postprocess/semantic.py 的 glossary_fix（AI 校正使用，會記下取代了什麼）。
