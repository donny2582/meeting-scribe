"""會議紀錄 .docx（SPEC 9.1、9.2）。

版面依三份參考文件實際量測：
- A4 直式；邊界上 2.5、下 2.0、左右 3.0 公分；中文標楷體、英數 Times New Roman。
- 會議名稱 18 pt 粗體置中（可兩行）；壹～陸各行 16 pt 粗體、固定行高 28 pt。
- 內文 14 pt、左右對齊、固定行高 24 pt、凸排：
  一、左 2.0／凸 1.0 公分；(一) 左 3.0／凸 0.85；1. 左 3.85／凸 0.7；(1) 左 4.6／凸 0.75。
- 編號以文字直接寫入（不使用 Word 自動編號），紀錄人員修改時不會錯亂。

回溯註解：每一條結論加上 Word 註解「依據：00:12:34–00:13:10 講者3；…」；
標「（待查證）」的條目以黃色醒目提示，並在註解說明原因。校對完成後在 Word「刪除文件中的所有註解」即可陳核。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX, WD_LINE_SPACING
from docx.shared import Cm, Pt

from meeting_scribe.models import Summary, SummaryItem
from meeting_scribe.render.docx_transcript import _set_font, available_path, output_filename
from meeting_scribe.render.minutes_text import cn_number
from meeting_scribe.render.roc_date import format_roc_date, format_roc_time, parse_hhmm
from meeting_scribe.transcript_io import TranscriptDocument

HEAVENLY = "壹貳參肆伍陸柒捌玖拾"
#: 各層級的（左縮排, 凸排）公分：一、 (一) 1. (1)
LEVEL_INDENT = [(2.0, 1.0), (3.0, 0.85), (3.85, 0.7), (4.6, 0.75)]
AUTHOR = "MeetingScribe"

Times = dict[str, tuple[float, float, str]]  # 片段編號 →（開始秒, 結束秒, 講者名稱）


def _hms(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def marker(level: int, n: int) -> str:
    """一、 (一) 1. (1)（依公文慣例）。"""
    return [f"{cn_number(n)}、", f"({cn_number(n)})", f"{n}.", f"({n})"][min(level, 3)]


def evidence_note(ids: list[str], times: Times) -> str:
    """依據：00:12:34–00:13:10 講者3；00:15:02–00:15:20 講者1。"""
    parts = []
    for i in ids:
        if i in times:
            start, end, name = times[i]
            span = f"{_hms(start)}–{_hms(end)}" if end > start else _hms(start)
            parts.append(f"{span} {name}")
    return "依據：" + "；".join(parts) if parts else ""


class MinutesWriter:
    """會議紀錄 .docx 產生器。"""

    def __init__(self, template: Path | None = None, trace_comments: bool = True) -> None:
        if template and template.is_file():
            self.d = Document(str(template))
            body = self.d.element.body
            for child in list(body):
                if not child.tag.endswith("sectPr"):
                    body.remove(child)
        else:
            self.d = Document()
        self.trace = trace_comments
        sec = self.d.sections[0]
        sec.page_height, sec.page_width = Cm(29.7), Cm(21.0)
        sec.top_margin, sec.bottom_margin = Cm(2.5), Cm(2.0)
        sec.left_margin = sec.right_margin = Cm(3.0)

    # ---- 段落 ----
    def _para(self, line_pt: float, align: Any = None, left: float | None = None, hanging: float | None = None,
              before: float = 0.0) -> Any:
        p = self.d.add_paragraph()
        f = p.paragraph_format
        f.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        f.line_spacing = Pt(line_pt)
        f.space_after = Pt(0)
        f.space_before = Pt(before)
        if align is not None:
            p.alignment = align
        if left is not None:
            f.left_indent = Cm(left)
            f.first_line_indent = Cm(-(hanging or 0))
        return p

    def title(self, text: str, size: float = 18) -> Any:
        runs = []
        for line in [ln.strip() for ln in text.splitlines() if ln.strip()] or ["未命名會議"]:
            p = self._para(28, WD_ALIGN_PARAGRAPH.CENTER)
            run = p.add_run(line)
            _set_font(run, size, bold=True)
            runs.append(run)
        return runs

    def heading(self, text: str) -> Any:
        """壹、會議時間：…（16 pt 粗體、固定行高 28 pt）。"""
        p = self._para(28)
        _set_font(p.add_run(text), 16, bold=True)
        return p

    def body(self, level: int, text: str, n: int, pending: bool = False, note: str = "",
             reason: str = "") -> Any:
        """內文條目（14 pt、左右對齊、凸排）；note 為回溯註解，pending 時黃色醒目提示。"""
        left, hanging = LEVEL_INDENT[min(level, 3)]
        p = self._para(24, WD_ALIGN_PARAGRAPH.JUSTIFY, left, hanging, before=3.6 if level == 0 else 0)
        run = p.add_run(f"{marker(level, n)}{text}")
        _set_font(run, 14)
        runs = [run]
        if pending:
            tag = p.add_run("（待查證）")
            _set_font(tag, 14, bold=True)
            for r in (run, tag):
                r.font.highlight_color = WD_COLOR_INDEX.YELLOW
            runs.append(tag)
        comment = note
        if pending:
            comment = (comment + "\n" if comment else "") + f"待查證：{reason or '請對照逐字稿確認'}"
        if self.trace and comment:
            self.d.add_comment(runs, text=comment, author=AUTHOR, initials="MS")
        return p

    def item_tree(self, item: SummaryItem, level: int, n: int, times: Times) -> None:
        self.body(level, item.text, n, item.pending, evidence_note(item.evidence, times), item.pending_reason)
        for k, child in enumerate(item.children, start=1):
            self.item_tree(child, level + 1, k, times)

    def action_table(self, summary: Summary, times: Times) -> None:
        p = self._para(28, before=12)
        _set_font(p.add_run("待辦事項追蹤表"), 16, bold=True)
        table = self.d.add_table(rows=1, cols=5)
        table.style = "Table Grid"
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        widths = [Cm(1.3), Cm(6.4), Cm(2.8), Cm(2.4), Cm(2.1)]
        for cell, text, w in zip(table.rows[0].cells, ("項次", "事項", "負責單位", "期限", "依據時間"), widths,
                                 strict=True):
            cell.width = w
            cell.text = ""
            _set_font(cell.paragraphs[0].add_run(text), 12, bold=True)
            cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        for n, a in enumerate(summary.action_items, start=1):
            row = table.add_row().cells
            ev = "；".join(_hms(times[i][0]) for i in a.evidence if i in times)
            values = (str(n), a.task + ("（待查證）" if a.pending else ""), a.owner, a.due, ev)
            for cell, text, w in zip(row, values, widths, strict=True):
                cell.width = w
                cell.text = ""
                run = cell.paragraphs[0].add_run(text)
                _set_font(run, 12)
                if a.pending and text is values[1]:
                    run.font.highlight_color = WD_COLOR_INDEX.YELLOW
            row[0].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        target = available_path(path)
        self.d.save(str(target))
        return target


def render_minutes(
    doc: TranscriptDocument, summary: Summary, times: Times, out_dir: Path, cfg: dict[str, Any] | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> Path:
    """產出會議紀錄 .docx，回傳檔案路徑。"""
    cfg = cfg or {}
    mcfg = cfg.get("minutes", {})
    info = doc.info

    def report(pct: float, msg: str) -> None:
        if progress:
            progress(pct, msg)

    template = Path(mcfg["template_path"]) if mcfg.get("template_path") else None
    w = MinutesWriter(template, bool(cfg.get("general", {}).get("keep_trace_comments", True)))
    report(5, "會議紀錄 .docx：表頭（會議名稱、時間、地點、主席、紀錄）")
    title_runs = w.title(info.title, float(mcfg.get("title_pt", 18)))
    if w.trace and summary.warnings:
        w.d.add_comment(title_runs[0], author=AUTHOR, initials="MS",
                        text="AI 提醒的不確定之處（請人工確認）：\n" + "\n".join(f"・{x}" for x in summary.warnings))
    d = date.fromisoformat(info.date)
    start, end = parse_hhmm(info.start_time), parse_hhmm(info.end_time)
    w.heading(f"壹、會議時間：{format_roc_date(d)}{format_roc_time(start) if start else ''}")
    w.heading(f"貳、會議地點：{info.location}")
    w.heading(f"參、主席：{info.chair}{'　' * 5}紀錄：{info.recorder}")
    w.heading(f"肆、參加人員：{info.attendees or '（詳簽到簿）'}")

    if info.want_discussion and summary.discussion_items:
        report(20, f"會議紀錄 .docx：討論事項（{len(summary.discussion_items)} 項）")
        w.heading("伍、討論事項：")
        for n, item in enumerate(summary.discussion_items, start=1):
            w.body(0, item.title, n, note=evidence_note(item.evidence, times))
            for k, child in enumerate(item.children, start=1):
                w.body(1, child, k)
    else:
        w.heading("伍、討論事項：（略）")

    if info.want_conclusion:
        total = sum(len(s.items) for s in summary.conclusions)
        report(45, f"會議紀錄 .docx：結論（{len(summary.conclusions)} 大項、{total} 條，每條加上依據時間碼註解）")
        w.heading("陸、結論：" if mcfg.get("conclusion_numbered", True) else "結論：")
        for n, section in enumerate(summary.conclusions, start=1):
            w.body(0, section.title, n)
            for k, item in enumerate(section.items, start=1):
                w.item_tree(item, 1, k, times)

    if info.want_action_table and summary.action_items:
        report(80, f"會議紀錄 .docx：待辦事項追蹤表（{len(summary.action_items)} 項）")
        w.action_table(summary, times)

    w.heading(f"散會({format_roc_time(end)})" if end else "散會")
    path = out_dir / output_filename("會議紀錄", info.title, d, ".docx")
    report(95, f"儲存檔案：{path.name}")
    return w.save(path)
