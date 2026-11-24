"""民國年、星期、時間的格式化（SPEC 4.1、9.2）。

例：115年11月9日(星期二)、下午2時00分、散會(下午4時30分)。
"""

from __future__ import annotations

from datetime import date, datetime, time

WEEKDAYS = "一二三四五六日"


def roc_year(d: date) -> int:
    """西元年 → 民國年。"""
    return d.year - 1911


def format_roc_date(d: date, weekday: bool = True) -> str:
    """115年11月9日(星期二)。"""
    text = f"{roc_year(d)}年{d.month}月{d.day}日"
    return text + f"(星期{WEEKDAYS[d.weekday()]})" if weekday else text


def format_roc_time(t: time) -> str:
    """下午2時00分；中午 12 點為「中午12時」，0 點為「上午0時」。"""
    if t.hour == 12:
        prefix, hour = "中午", 12
    elif t.hour > 12:
        prefix, hour = "下午", t.hour - 12
    else:
        prefix, hour = "上午", t.hour
    return f"{prefix}{hour}時{t.minute:02d}分"


def format_roc_ymd(d: date) -> str:
    """檔名用的民國年月日：1151109。"""
    return f"{roc_year(d)}{d.month:02d}{d.day:02d}"


def format_roc_datetime(dt: datetime) -> str:
    """產製時間：115年11月23日 16:40。"""
    return f"{format_roc_date(dt.date(), weekday=False)} {dt:%H:%M}"


def round_to_minutes(t: time, step: int = 5) -> time:
    """四捨五入到 step 分鐘（散會時間預設值用）。"""
    total = t.hour * 60 + t.minute + (1 if t.second >= 30 else 0)
    total = int(round(total / step) * step) % (24 * 60)
    return time(total // 60, total % 60)


def parse_hhmm(text: str) -> time | None:
    """「14:00」→ time；格式不符回傳 None。"""
    try:
        h, m = text.split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        return None
