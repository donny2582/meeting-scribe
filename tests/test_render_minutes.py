"""第 3 階段：會議紀錄 .docx 版面、Word 註解、待查證標示、心智圖 PNG 與 XMind。"""

import json
import zipfile
from pathlib import Path

import docx
from docx.enum.text import WD_COLOR_INDEX
from PIL import Image

from meeting_scribe.models import MindNode, RawSegment, Summary
from meeting_scribe.render.docx_minutes import evidence_note, marker, render_minutes
from meeting_scribe.render.mindmap_png import render_mindmap_png, wrap
from meeting_scribe.render.mindmap_xmind import render_mindmap_xmind
from meeting_scribe.transcript_io import TranscriptDocument

EA = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}eastAsia"


def _doc(tmp_path: Path) -> TranscriptDocument:
    segs = [RawSegment(start=60 * i, end=60 * i + 20, speaker=f"講者{i % 3 + 1}", text="內容") for i in range(5)]
    d = TranscriptDocument(source="t", folder=str(tmp_path), segments=segs)
    d.info.title = "交通部116年道路交通安全推動計畫（草案）\n第2次研商會議紀錄"
    d.info.date, d.info.start_time, d.info.end_time = "2026-11-09", "14:00", "16:30"
    d.info.location, d.info.chair, d.info.recorder = "交通部16樓1609會議室", "交通部陳次長彥伯", "陳祈潔"
    d.info.want_action_table = True
    return d


def _summary() -> Summary:
    return Summary.model_validate({
        "meeting_type": "agenda",
        "discussion_items": [{"title": "毒駕防制納入推動計畫", "children": [], "evidence": ["S0001"]}],
        "conclusions": [{"title": "毒駕防制納入計畫", "items": [
            {"role": "單位意見", "speaker": "公共運輸及監理司", "text": "公共運輸及監理司：毒駕修法將於今年底完成。",
             "evidence": ["S0002"], "children": [
                 {"role": "單位意見", "text": "修法通過後配合執法程序。", "evidence": ["S0002"], "children": [
                     {"role": "單位意見", "text": "第三層內容。", "evidence": ["S0003"]}]}]},
            {"role": "主席裁示", "text": "主席裁示：（未明確裁示）", "evidence": [], "pending": True,
             "pending_reason": "主席未明確裁示"},
        ]}],
        "action_items": [{"task": "提送修正版", "owner": "公路局", "due": "未提及", "evidence": ["S0004"]}],
        "mindmap": {"text": "研商會", "children": [{"text": "毒駕防制", "children": [
            {"text": "暫不納入116年計畫", "children": [{"text": "公路運輸及監理司：今年底完成修法", "children": []}]}]}]},
        "warnings": ["講者2 身分不確定"],
    })


def _times(d: TranscriptDocument) -> dict:
    return {f"S{i + 1:04d}": (s.start, s.end, d.display_name(s.speaker)) for i, s in enumerate(d.segments)}


def test_markers_and_evidence_note(tmp_path: Path) -> None:
    assert [marker(lv, 2) for lv in range(4)] == ["二、", "(二)", "2.", "(2)"]
    d = _doc(tmp_path)
    assert evidence_note(["S0002", "S9999"], _times(d)) == "依據：00:01:00–00:01:20 講者2"


def test_minutes_layout_matches_reference(tmp_path: Path) -> None:
    d = _doc(tmp_path)
    path = render_minutes(d, _summary(), _times(d), tmp_path / "out")
    assert path.name == "會議紀錄_交通部116年道路交通安全推動計畫（草案_1151109.docx"
    doc = docx.Document(str(path))
    sec = doc.sections[0]
    assert (round(sec.top_margin.cm, 1), round(sec.bottom_margin.cm, 1), round(sec.left_margin.cm, 1),
            round(sec.right_margin.cm, 1)) == (2.5, 2.0, 3.0, 3.0)
    paras = [p for p in doc.paragraphs if p.text]
    texts = [p.text for p in paras]
    assert texts[:2] == ["交通部116年道路交通安全推動計畫（草案）", "第2次研商會議紀錄"]  # 兩行標題
    assert texts[2] == "壹、會議時間：115年11月9日(星期一)下午2時00分"
    assert texts[4].startswith("參、主席：交通部陳次長彥伯") and texts[4].endswith("紀錄：陳祈潔")
    for expected in ("一、毒駕防制納入推動計畫", "陸、結論：", "(一)公共運輸及監理司：毒駕修法將於今年底完成。",
                     "1.修法通過後配合執法程序。", "(1)第三層內容。", "散會(下午4時30分)"):
        assert expected in texts, expected

    def by_text(prefix: str):  # type: ignore[no-untyped-def]
        return next(p for p in paras if p.text.startswith(prefix))

    title, head, lvl0, lvl1 = by_text("交通部116"), by_text("壹、"), by_text("一、毒駕"), by_text("(一)公共")
    assert title.runs[0].font.size.pt == 18 and title.runs[0].font.bold and title.alignment == 1
    assert head.runs[0].font.size.pt == 16 and head.runs[0].font.bold and head.paragraph_format.line_spacing.pt == 28
    assert lvl0.runs[0].font.size.pt == 14 and lvl0.paragraph_format.line_spacing.pt == 24
    assert round(lvl0.paragraph_format.left_indent.cm, 2) == 2.0
    assert round(lvl0.paragraph_format.first_line_indent.cm, 2) == -1.0
    assert round(lvl1.paragraph_format.left_indent.cm, 2) == 3.0
    assert round(lvl1.paragraph_format.first_line_indent.cm, 2) == -0.85
    assert lvl1.runs[0]._element.rPr.rFonts.get(EA) == "標楷體"
    assert all(p._p.pPr is None or p._p.pPr.numPr is None for p in paras)  # 編號是文字，不用自動編號

    pending = by_text("(二)主席裁示")
    assert pending.text.endswith("（待查證）")
    assert all(r.font.highlight_color == WD_COLOR_INDEX.YELLOW for r in pending.runs if r.text)  # 註解參照不算
    comments = {c.text for c in doc.comments}
    assert "依據：00:01:00–00:01:20 講者2" in comments
    assert "待查證：主席未明確裁示" in comments
    assert any("講者2 身分不確定" in c for c in comments)
    assert [c.text for c in doc.tables[0].rows[0].cells] == ["項次", "事項", "負責單位", "期限", "依據時間"]


def test_minutes_without_trace_comments_and_unchecked_items(tmp_path: Path) -> None:
    d = _doc(tmp_path)
    d.info.want_discussion = d.info.want_action_table = False
    cfg = {"general": {"keep_trace_comments": False}, "minutes": {"conclusion_numbered": False}}
    doc = docx.Document(str(render_minutes(d, _summary(), _times(d), tmp_path / "out", cfg)))
    texts = [p.text for p in doc.paragraphs]
    assert "伍、討論事項：（略）" in texts and "結論：" in texts
    assert not list(doc.comments) and not doc.tables


def test_mindmap_png(tmp_path: Path) -> None:
    root = _summary().mindmap
    path = render_mindmap_png(root, tmp_path / "心智圖.png")
    with Image.open(path) as img:
        assert img.width >= 2400 and img.height >= 900
    big = MindNode(text="根", children=[MindNode(text=f"議題{i}", children=[MindNode(text=f"要點{j}") for j in range(4)])
                                       for i in range(12)])
    with Image.open(render_mindmap_png(big, tmp_path / "big.png")) as img:
        assert img.height > 2000  # 節點多時自動加高
    assert wrap("一" * 45) == ["一" * 20, "一" * 20, "一" * 5]
    assert wrap("交通部116年道路交通安全推動計畫（草案）\n第2次研商會議紀錄") == [
        "交通部116年道路交通安全推動計畫（草案）", "第2次研商會議紀錄"]  # 保留原換行、「）」不放行首
    assert wrap("一" * 20 + "），好") == ["一" * 20 + "），", "好"]


def test_mindmap_xmind(tmp_path: Path) -> None:
    path = render_mindmap_xmind(_summary().mindmap, tmp_path / "心智圖.xmind")
    with zipfile.ZipFile(path) as z:
        assert set(z.namelist()) == {"content.json", "metadata.json", "manifest.json"}
        content = json.loads(z.read("content.json"))
        manifest = json.loads(z.read("manifest.json"))
    root = content[0]["rootTopic"]
    assert content[0]["class"] == "sheet" and root["title"] == "研商會"
    assert root["structureClass"] == "org.xmind.ui.logic.right"
    l1 = root["children"]["attached"][0]
    assert l1["title"] == "毒駕防制" and l1["style"]["properties"]["svg:fill"].startswith("#")
    assert l1["children"]["attached"][0]["children"]["attached"][0]["title"] == "公路運輸及監理司：今年底完成修法"
    assert "content.json" in manifest["file-entries"]
