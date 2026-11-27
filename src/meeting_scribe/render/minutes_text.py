"""公文編號用的國字數字（會議紀錄 .docx 使用）。

原本第 2 階段的「會議紀錄草稿（純文字預覽）」已由第 3 階段的會議紀錄 .docx 取代，預覽功能已移除。
"""

from __future__ import annotations

CN_DIGITS = "零一二三四五六七八九"


def cn_number(n: int) -> str:
    """1 → 一、11 → 十一、25 → 二十五（公文編號用）。"""
    if n < 10:
        return CN_DIGITS[n]
    tens, ones = divmod(n, 10)
    head = "" if tens == 1 else CN_DIGITS[tens]
    return f"{head}十{CN_DIGITS[ones] if ones else ''}"
