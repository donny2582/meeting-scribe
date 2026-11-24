"""逐字稿匯入、文件存讀、民國日期、逐字稿 .docx。"""

from datetime import date, datetime, time
from pathlib import Path

import docx
import pytest

from meeting_scribe.models import RawSegment, SttResult
from meeting_scribe.render.docx_transcript import (
    available_path,
    merged_blocks,
    output_filename,
    render_transcript,
    speaker_stats,
)
from meeting_scribe.render.roc_date import (
    format_roc_date,
    format_roc_datetime,
    format_roc_time,
    format_roc_ymd,
    parse_hhmm,
    round_to_minutes,
)
from meeting_scribe.transcript_io import (
    ImportError_,
    TranscriptDocument,
    from_stt_result,
    import_transcript,
    parse_lines,
    parse_srt,
)


# ---- 民國日期 ----
def test_roc_date_and_time() -> None:
    assert format_roc_date(date(2026, 11, 9)) == "115年11月9日(星期一)"
    assert format_roc_date(date(2026, 11, 10), weekday=False) == "115年11月10日"
    assert format_roc_time(time(14, 0)) == "下午2時00分"
    assert format_roc_time(time(9, 5)) == "上午9時05分"
    assert format_roc_time(time(12, 30)) == "中午12時30分"
    assert format_roc_ymd(date(2026, 11, 9)) == "1151109"
    assert format_roc_datetime(datetime(2026, 11, 23, 16, 40)) == "115年11月23日 16:40"
    assert round_to_minutes(time(16, 28)) == time(16, 30)
    assert round_to_minutes(time(16, 32, 10)) == time(16, 30)
    assert parse_hhmm("14:05") == time(14, 5) and parse_hhmm("abc") is None


# ---- 匯入 ----
def test_parse_our_draft_format() -> None:
    lines = ["辨識引擎：Soniox", "錄音長度：01:00:00", "",
             "[00:00:12] 講者1：好，我們開始。", "[00:00:45] 講者2（疑似）：請公路局說明。",
             "補充一句。", "[00:01:10] 公路局：好的。"]
    segs, timed = parse_lines(lines)
    assert timed
    assert [(s.start, s.speaker, s.speaker_uncertain) for s in segs] == [
        (12, "講者1", False), (45, "講者2", True), (70, "公路局", False)]
    assert segs[1].text == "請公路局說明。\n補充一句。"
    assert segs[0].end == 45  # 結束時間＝下一段開始


def test_parse_user_meeting_software_format() -> None:
    """使用者提供的格式：時間碼（無括號）＋講者一行，內容在下一行。"""
    lines = [
        "00:04:07 演講者 1",
        ("胎文的資訊照明的資訊，這個恐怕都是在目前的事故調查表裡面是反映不出來的。如果我的理解是正確的話，"
         "那所以A one先處理那其他的我覺得可以再再大家再一起即時廣播。以上要圍我們。"),
        "00:04:27 演講者 2",
        "謝謝委員的提議啦，其實我對委員是有道法的，因為從我們在看警方的數據裡面，其實我們常常有一些疑惑喔，",
        "那我也同意，就是委員剛才講，其實東西我覺得不是警員的錯，你沒有辦法再苛責他的啦，因為他在現場。",
    ]
    segs, timed = parse_lines(lines)
    assert timed
    assert [(s.start, s.end, s.speaker) for s in segs] == [(247, 267, "講者1"), (267, 267, "講者2")]
    assert segs[0].text.startswith("胎文的資訊") and segs[0].text.endswith("以上要圍我們。")
    assert segs[1].text.count("\n") == 1 and "因為他在現場。" in segs[1].text


@pytest.mark.parametrize("lines,expected", [
    # 講者在時間碼前面
    (["演講者 1  00:04:07", "第一句。", "Speaker 2 (00:04:27)", "第二句。"],
     [(247, "講者1", "第一句。"), (267, "講者2", "第二句。")]),
    # 只有分:秒、括號、全形冒號
    (["（4：07） 主席：開始開會。", "[4:27] 公路局：報告。"],
     [(247, "主席", "開始開會。"), (267, "公路局", "報告。")]),
    # 時間碼與講者之間有分隔符號、講者（疑似）
    (["00:00:05 - 講者3（疑似）", "嗯，好。"], [(5, "講者3", "嗯，好。")]),
    # 內容裡的「1:20」不是時間碼（檔案時間碼都有「時」）
    (["00:01:00 說話者 1", "這個", "1:20 的比例很高", "00:01:30 說話者 2", "了解。"],
     [(60, "講者1", "這個\n1:20 的比例很高"), (90, "講者2", "了解。")]),
    # 時間倒退的行視為內容
    (["00:10:00 主席：大家好", "00:02:00 這只是內容裡提到的時間", "00:10:30 主席：開始"],
     [(600, "主席", "大家好\n00:02:00 這只是內容裡提到的時間"), (630, "主席", "開始")]),
])
def test_parse_tolerant_variants(lines: list[str], expected: list[tuple[float, str, str]]) -> None:
    segs, timed = parse_lines(lines)
    assert timed
    assert [(s.start, s.speaker, s.text) for s in segs] == expected


def test_uncertain_flag_kept() -> None:
    segs, _ = parse_lines(["00:00:05 - 講者3（疑似）", "嗯，好。"])
    assert segs[0].speaker_uncertain


def test_parse_plain_lines_without_timecodes() -> None:
    segs, timed = parse_lines(["主席：開會。", "", "這是一段沒有講者的文字"])
    assert not timed
    assert [(s.speaker, s.text) for s in segs] == [("主席", "開會。"), ("講者", "這是一段沒有講者的文字")]


def test_parse_srt() -> None:
    srt = "1\n00:00:01,500 --> 00:00:03,000\n講者1：大家好\n\n2\n00:00:04,000 --> 00:00:06,250\n第二句\n"
    segs = parse_srt(srt)
    assert [(s.start, s.end, s.speaker, s.text) for s in segs] == [
        (1.5, 3.0, "講者1", "大家好"), (4.0, 6.25, "講者", "第二句")]


def test_import_txt_big5_and_docx(tmp_path: Path) -> None:
    txt = tmp_path / "會議.txt"
    txt.write_bytes("[00:00:01] 主席：開始開會\n".encode("cp950"))
    doc = import_transcript(txt, tmp_path / "job")
    assert doc.segments[0].text == "開始開會" and doc.has_timecodes

    d = docx.Document()
    d.add_paragraph("[00:00:05] 講者1：第一段")
    d.add_paragraph("[00:00:09] 講者2：第二段")
    path = tmp_path / "逐字稿.docx"
    d.save(str(path))
    doc = import_transcript(path, tmp_path / "job2")
    assert [s.speaker for s in doc.segments] == ["講者1", "講者2"]


def test_import_rejects_bad_files(tmp_path: Path) -> None:
    bad = tmp_path / "a.pdf"
    bad.write_text("x")
    with pytest.raises(ImportError_, match="不支援"):
        import_transcript(bad, tmp_path)
    empty = tmp_path / "empty.txt"
    empty.write_text("\n\n", encoding="utf-8")
    with pytest.raises(ImportError_, match="沒有可用"):
        import_transcript(empty, tmp_path)


def _result() -> SttResult:
    segs = [RawSegment(start=0, end=2, speaker="講者1", text="好，我們開始。"),
            RawSegment(start=2.5, end=4, speaker="講者1", text="第一案。"),
            RawSegment(start=10, end=12, speaker="講者2", text="Our team", speaker_uncertain=True),
            RawSegment(start=12.5, end=14, speaker="講者2", text="agrees.")]
    return SttResult(engine="soniox", engine_version="Soniox stt-async-v6", audio_duration=3600, elapsed_sec=1,
                     locales=["zh-TW"], segments=segs)


def test_document_from_result_save_and_reload(tmp_path: Path) -> None:
    audio = tmp_path / "會議.m4a"
    audio.write_bytes(b"x")
    doc = from_stt_result(_result(), tmp_path / "job", audio)
    assert doc.audio_name == "會議.m4a" and doc.info.date == date.today().isoformat()
    doc.rename_speaker("講者2", "公路局")
    doc.info.title = "第2次研商會議"
    doc.save()
    again = from_stt_result(_result(), tmp_path / "job", audio)  # 已存的修改優先
    assert again.display_name("講者2") == "公路局" and again.info.title == "第2次研商會議"


def test_merge_speaker() -> None:
    doc = TranscriptDocument(source="t", segments=list(_result().segments))
    doc.rename_speaker("講者2", "公路局")
    assert doc.merge_speaker("講者2", "講者1") == 2
    assert doc.speakers() == ["講者1"] and "講者2" not in doc.speaker_names and doc.edited


# ---- 逐字稿 .docx ----
def test_merged_blocks_and_stats() -> None:
    doc = TranscriptDocument(source="t", segments=list(_result().segments))
    blocks = merged_blocks(doc)
    assert [(b[0], b[1], b[2], b[3]) for b in blocks] == [
        (0, "講者1", False, "好，我們開始。第一案。"), (10, "講者2", True, "Our team agrees.")]
    assert speaker_stats(doc) == [("講者1", 2, 3.5), ("講者2", 2, 3.5)]


def test_output_filename_and_locked_file(tmp_path: Path) -> None:
    name = output_filename("逐字稿", '交通部「道安」計畫:第2次/研商會議紀錄超過二十個字的名稱', date(2026, 11, 9), ".docx")
    assert name.startswith("逐字稿_交通部「道安」計畫第2次研商會議紀") and name.endswith("_1151109.docx")
    assert available_path(tmp_path / "a.docx") == tmp_path / "a.docx"


def test_render_transcript(tmp_path: Path) -> None:
    doc = from_stt_result(_result(), tmp_path / "job")
    doc.info.title = "道安業務研商會議"
    doc.info.date = "2026-11-09"
    doc.info.location = "交通部第1會議室"
    doc.rename_speaker("講者2", "公路局")
    path = render_transcript(doc, tmp_path / "out", now=datetime(2026, 11, 23, 16, 40))
    assert path.name == "逐字稿_道安業務研商會議_1151109.docx"
    d = docx.Document(str(path))
    texts = [p.text for p in d.paragraphs]
    assert texts[0] == "道安業務研商會議逐字稿"
    assert "115年11月9日(星期一)下午2時00分" in texts[1] and "交通部第1會議室" in texts[1]
    made = next(t for t in texts if t.startswith("產製時間"))
    assert "115年11月23日 16:40" in made and "Soniox" in made
    assert "[00:00:10] 公路局（疑似）：" in texts
    assert "好，我們開始。第一案。" in texts
    table = d.tables[0]
    assert [c.text for c in table.rows[2].cells][:4] == ["講者2", "2", "00:00:03", "公路局"]
    run = d.paragraphs[0].runs[0]
    assert run.font.bold and run.font.size.pt == 16
    assert run._element.rPr.rFonts.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}eastAsia") == "標楷體"
    sec = d.sections[0]
    assert round(sec.left_margin.cm, 1) == 3.0 and round(sec.top_margin.cm, 1) == 2.5
