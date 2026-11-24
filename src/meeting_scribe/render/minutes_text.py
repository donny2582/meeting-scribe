"""會議紀錄草稿（純文字預覽，SPEC 9.2 的結構）。

第 2 階段先用純文字呈現 AI 整理的結果，方便核對內容與依據時間碼；
第 3 階段再依公文格式產出 .docx（標楷體、Word 註解回溯）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from meeting_scribe.models import Summary, SummaryItem
from meeting_scribe.render.roc_date import format_roc_date, format_roc_time, parse_hhmm

CN_DIGITS = "零一二三四五六七八九"


def cn_number(n: int) -> str:
    """1 → 一、11 → 十一、25 → 二十五（公文編號用）。"""
    if n < 10:
        return CN_DIGITS[n]
    tens, ones = divmod(n, 10)
    head = "" if tens == 1 else CN_DIGITS[tens]
    return f"{head}十{CN_DIGITS[ones] if ones else ''}"


HEAVENLY = "壹貳參肆伍陸柒捌玖拾"


def _hms(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def evidence_text(ids: list[str], times: dict[str, tuple[float, str]]) -> str:
    """依據：00:12:34 講者3；00:15:02 講者1。"""
    parts = [f"{_hms(times[i][0])} {times[i][1]}" for i in ids if i in times]
    return "；".join(parts)


def _marker(level: int, n: int) -> str:
    return [f"{cn_number(n)}、", f"({cn_number(n)})", f"{n}.", f"({n})"][min(level, 3)]


def _item_lines(item: SummaryItem, level: int, n: int, times: dict[str, tuple[float, str]],
                indent: str) -> list[str]:
    pad = indent + "    " * level
    pending = "（待查證）" if item.pending else ""
    ev = evidence_text(item.evidence, times)
    note = f"　〔依據：{ev}〕" if ev else ""
    reason = f"　〔待查證原因：{item.pending_reason}〕" if item.pending and item.pending_reason else ""
    lines = [f"{pad}{_marker(level, n)} {item.text}{pending}{note}{reason}"]
    for k, child in enumerate(item.children, start=1):
        lines += _item_lines(child, level + 1, k, times, indent)
    return lines


def render_minutes_text(summary: Summary, info: dict, times: dict[str, tuple[float, str]],
                        want: dict[str, bool]) -> str:
    """產生會議紀錄草稿文字。times：片段編號 →（開始秒, 講者名稱）。"""
    out: list[str] = []
    title = (info.get("title") or "未命名會議").strip()
    out += [title, ""]
    d = date.fromisoformat(info["date"]) if info.get("date") else None
    start, end = parse_hhmm(info.get("start_time", "")), parse_hhmm(info.get("end_time", ""))
    when = (format_roc_date(d) if d else "") + (format_roc_time(start) if start else "")
    out.append(f"壹、會議時間：{when}")
    out.append(f"貳、會議地點：{info.get('location') or ''}")
    out.append(f"參、主席：{info.get('chair') or ''}　　　　　紀錄：{info.get('recorder') or ''}")
    out.append(f"肆、參加人員：{info.get('attendees') or '（詳簽到簿）'}")
    if want.get("discussion", True) and summary.discussion_items:
        out.append("伍、討論事項：")
        for n, d_item in enumerate(summary.discussion_items, start=1):
            ev = evidence_text(d_item.evidence, times)
            out.append(f"    {cn_number(n)}、{d_item.title}" + (f"　〔依據：{ev}〕" if ev else ""))
            for k, child in enumerate(d_item.children, start=1):
                out.append(f"        ({cn_number(k)}) {child}")
    else:
        out.append("伍、討論事項：（略）")
    if want.get("conclusion", True):
        out.append("陸、結論：")
        for n, section in enumerate(summary.conclusions, start=1):
            out.append(f"    {cn_number(n)}、{section.title}")
            for k, item in enumerate(section.items, start=1):
                out += _item_lines(item, 1, k, times, "    ")
    if want.get("action_table") and summary.action_items:
        out += ["", "待辦事項追蹤表", "項次｜事項｜負責單位｜期限｜依據時間"]
        for n, a in enumerate(summary.action_items, start=1):
            ev = evidence_text(a.evidence, times)
            pending = "（待查證）" if a.pending else ""
            out.append(f"{n}｜{a.task}{pending}｜{a.owner}｜{a.due}｜{ev}")
    out += ["", f"散會({format_roc_time(end)})" if end else "散會"]
    if want.get("mindmap") and summary.mindmap.text:
        out += ["", "────────", "心智圖大綱（第 3 階段產出 PNG 與 XMind）", summary.mindmap.text]

        def walk(node, depth: int) -> None:  # type: ignore[no-untyped-def]
            for child in node.children:
                out.append("    " * depth + "・" + child.text)
                walk(child, depth + 1)

        walk(summary.mindmap, 1)
    if summary.warnings:
        out += ["", "────────", "AI 提醒的不確定之處（請人工確認）："]
        out += [f"・{w}" for w in summary.warnings]
    out += ["", "────────",
            "說明：本草稿由 AI 依逐字稿整理，〔依據〕為對應的逐字稿時間碼；標「（待查證）」者請對照逐字稿確認。",
            f"整理模型：{summary.model}"]
    return "\n".join(out) + "\n"


def write_minutes_text(text: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8-sig")
    return path
