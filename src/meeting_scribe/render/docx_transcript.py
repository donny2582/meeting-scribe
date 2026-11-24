"""逐字稿 .docx（SPEC 9.3）。

- 字型：中文標楷體、英數 Times New Roman；A4，邊界上 2.5、下 2.0、左右 3.0 公分。
- 內文 12 pt、固定行高 20 pt；標題 16 pt 粗體置中。
- 同一講者連續發言、間隔少於 2 秒時合併成一段，只顯示第一個時間碼。
- 講者有使用者命名時以命名顯示，講者對照表保留原代號。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

from meeting_scribe.render.roc_date import (
    format_roc_date,
    format_roc_datetime,
    format_roc_time,
    format_roc_ymd,
    parse_hhmm,
)
from meeting_scribe.transcript_io import TranscriptDocument

CJK_FONT = "標楷體"
LATIN_FONT = "Times New Roman"
MERGE_GAP_SEC = 2.0
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')


def _hms(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _set_font(run, size: float, bold: bool = False) -> None:  # type: ignore[no-untyped-def]
    run.font.name = LATIN_FONT
    run.font.size = Pt(size)
    run.font.bold = bold
    rpr = run._element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(fonts)
    fonts.set(qn("w:eastAsia"), CJK_FONT)
    fonts.set(qn("w:ascii"), LATIN_FONT)
    fonts.set(qn("w:hAnsi"), LATIN_FONT)


def _para(doc, text: str = "", size: float = 12, bold: bool = False, center: bool = False,  # type: ignore[no-untyped-def]
          line_pt: float = 20):
    p = doc.add_paragraph()
    fmt = p.paragraph_format
    fmt.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    fmt.line_spacing = Pt(line_pt)
    fmt.space_after = Pt(0)
    if center:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if text:
        _set_font(p.add_run(text), size, bold)
    return p


def merged_blocks(doc: TranscriptDocument) -> list[tuple[float, str, bool, str]]:
    """合併同一講者、間隔小於 2 秒的連續片段：[(開始秒, 講者, 是否疑似, 文字)]。"""
    blocks: list[tuple[float, str, bool, str]] = []
    last_end = -1.0
    for seg in doc.segments:
        gap_ok = doc.has_timecodes and seg.start - last_end < MERGE_GAP_SEC
        if blocks and blocks[-1][1] == seg.speaker and gap_ok:
            start, spk, unc, text = blocks[-1]
            sep = " " if text[-1:].isascii() and text[-1:].isalnum() and seg.text[:1].isascii() else ""
            blocks[-1] = (start, spk, unc or seg.speaker_uncertain, text + sep + seg.text)
        else:
            blocks.append((seg.start, seg.speaker, seg.speaker_uncertain, seg.text))
        last_end = seg.end
    return blocks


def speaker_stats(doc: TranscriptDocument) -> list[tuple[str, int, float]]:
    """講者對照表：[(講者代號, 發言次數, 發言總長秒)]。"""
    stats: dict[str, list[float]] = {}
    for seg in doc.segments:
        item = stats.setdefault(seg.speaker, [0, 0.0])
        item[0] += 1
        item[1] += max(0.0, seg.end - seg.start)
    return [(spk, int(v[0]), v[1]) for spk, v in stats.items()]


def output_filename(prefix: str, title: str, meeting_date: date, suffix: str) -> str:
    """{前綴}_{會議名稱前20字}_{民國年月日}{副檔名}，移除 Windows 不允許的字元。"""
    name = _BAD_CHARS.sub("", title.replace("\n", " ")).strip()[:20] or "未命名會議"
    return f"{prefix}_{name}_{format_roc_ymd(meeting_date)}{suffix}"


def available_path(path: Path) -> Path:
    """檔案被 Word 開著（無法覆寫）時改用 _2、_3… 的新檔名。"""
    candidate, n = path, 2
    while candidate.exists():
        try:
            with open(candidate, "a+b"):
                return candidate
        except PermissionError:
            candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
            n += 1
    return candidate


def render_transcript(doc: TranscriptDocument, out_dir: Path, now: datetime | None = None) -> Path:
    """產出逐字稿 .docx，回傳檔案路徑。"""
    info = doc.info
    meeting_date = date.fromisoformat(info.date)
    d = Document()
    section = d.sections[0]
    section.page_height, section.page_width = Cm(29.7), Cm(21.0)
    section.top_margin, section.bottom_margin = Cm(2.5), Cm(2.0)
    section.left_margin = section.right_margin = Cm(3.0)

    title = (info.title.strip() or "未命名會議").replace("\n", " ")
    _para(d, f"{title}逐字稿", size=16, bold=True, center=True, line_pt=28)
    start = parse_hhmm(info.start_time)
    when = format_roc_date(meeting_date) + (format_roc_time(start) if start else "")
    _para(d, f"會議時間：{when}　　會議地點：{info.location or '（未填）'}")
    if doc.audio_name:
        length = f"（長度 {_hms(doc.audio_duration)}）" if doc.audio_duration else ""
        _para(d, f"錄音檔：{doc.audio_name}{length}")
    engine = f"　　辨識引擎：{doc.engine}" if doc.engine else ""
    _para(d, f"產製時間：{format_roc_datetime(now or datetime.now())}{engine}")
    _para(d)

    _para(d, "講者對照表", bold=True)
    stats = speaker_stats(doc)
    table = d.add_table(rows=1, cols=5)
    table.style = "Table Grid"
    for cell, text in zip(table.rows[0].cells, ("講者", "發言次數", "發言總長", "使用者命名", "備註"), strict=True):
        cell.text = ""
        _set_font(cell.paragraphs[0].add_run(text), 12, bold=True)
    for spk, count, total in stats:
        row = table.add_row().cells
        values = (spk, str(count), _hms(total) if doc.has_timecodes else "—", doc.speaker_names.get(spk, ""), "")
        for cell, text in zip(row, values, strict=True):
            cell.text = ""
            _set_font(cell.paragraphs[0].add_run(text), 12)
    _para(d)
    _para(d, "說明：「（疑似）」表示講者辨識不確定；〔台語，辨識可能不準〕、〔辨識信心低〕表示需要重聽確認。")
    _para(d, "─" * 20)

    for start_sec, spk, uncertain, text in merged_blocks(doc):
        stamp = f"[{_hms(start_sec)}] " if doc.has_timecodes else ""
        head = f"{stamp}{doc.display_name(spk)}{'（疑似）' if uncertain else ''}："
        _para(d, head, bold=True)
        _para(d, text)
        _para(d)

    out_dir.mkdir(parents=True, exist_ok=True)
    path = available_path(out_dir / output_filename("逐字稿", info.title, meeting_date, ".docx"))
    d.save(str(path))
    return path
