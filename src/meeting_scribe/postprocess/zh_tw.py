"""中文字形轉換：簡體 → 臺灣繁體（含臺灣慣用詞，OpenCC s2twp）。

部分引擎（例如 Soniox）的中文只有 ``zh``，輸出可能是簡體或混用。這裡先以 OpenCC 做確定性的轉換，
確保逐字稿一定是繁體；之後第 1 階段的 LLM 會再做「臺灣在地化」校潤（見 postprocess/localize.py）。

只轉換中文片段：日文漢字（例：気、駅）若被轉換會變成錯字，因此由呼叫端依片段語言決定是否轉換。
"""

from __future__ import annotations

from functools import lru_cache

CONVERSION = "s2twp"


@lru_cache(maxsize=1)
def _converter():  # type: ignore[no-untyped-def]
    from opencc import OpenCC

    return OpenCC(CONVERSION)


def to_zh_tw(text: str) -> str:
    """把中文轉成臺灣繁體與臺灣慣用詞；已是繁體的文字維持不變。"""
    return _converter().convert(text) if text else text
