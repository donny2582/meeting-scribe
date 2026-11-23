"""贅字刪除（SPEC 6.4）——只刪不改。

本模組目前提供：
- 規則層：依各語言的贅字清單刪除，並把重複的「對對對」縮成「對」。
- 子序列驗證：確認清理後文字（忽略標點）只刪字、沒有新增或調換。

LLM 層於第 1 階段加入。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

_PUNCT_RUN = re.compile(r"([，。、！？,.!?；;：:])(?:\s*[，。、,；;])+")
_LEADING_PUNCT = re.compile(r"^[\s，。、,.；;：:！？!?]+")
_SPACES = re.compile(r"[ \t　]{2,}")


def _is_latin(word: str) -> bool:
    return all(ch.isascii() for ch in word)


def strip_punct(text: str) -> str:
    """移除標點符號與空白（供比對使用）。"""
    return "".join(
        ch for ch in text if not ch.isspace() and not unicodedata.category(ch).startswith(("P", "Z"))
    )


def remove_fillers_rule(text: str, fillers: Iterable[str], repeat_collapse: Iterable[str] = ()) -> str:
    """規則層贅字刪除。

    Args:
        text: 校正後的文字。
        fillers: 贅字清單（最長的優先比對）。
        repeat_collapse: 連續重複時縮成一個的詞，例如「對」。
    """
    result = text
    for word in repeat_collapse:
        if not word:
            continue
        sep = r"[\s，,、]*"
        flags = re.IGNORECASE if _is_latin(word) else 0
        pattern = re.compile(rf"(?:{re.escape(word)}{sep}){{1,}}{re.escape(word)}", flags)
        result = pattern.sub(word, result)
    for word in sorted({w for w in fillers if w}, key=len, reverse=True):
        if _is_latin(word):
            result = re.sub(rf"\b{re.escape(word)}\b[,]?\s*", "", result, flags=re.IGNORECASE)
        else:
            result = result.replace(word, "")
    result = _PUNCT_RUN.sub(r"\1", result)
    result = _LEADING_PUNCT.sub("", result)
    result = _SPACES.sub(" ", result)
    return result.strip()


def is_subsequence(clean: str, source: str) -> bool:
    """檢查 clean（忽略標點與空白）是否為 source 的子序列。"""
    it = iter(strip_punct(source).lower())
    return all(ch in it for ch in strip_punct(clean).lower())
