"""Word 輸出的語言與字型：比照參考文件設定臺灣繁體中文，讓手機版 Word 不會顯示亂碼。"""

import zipfile
from pathlib import Path

from meeting_scribe.models import RawSegment
from meeting_scribe.render.docx_common import new_document
from meeting_scribe.render.docx_transcript import render_transcript
from meeting_scribe.transcript_io import TranscriptDocument


def _check(path: Path) -> None:
    with zipfile.ZipFile(path) as z:
        styles = z.read("word/styles.xml").decode("utf-8")
        fonts = z.read("word/fontTable.xml").decode("utf-8")
        for name in z.namelist():
            if name.endswith(".xml"):
                assert z.read(name).startswith(b"<?xml version='1.0' encoding='UTF-8'") or \
                    z.read(name).startswith(b'<?xml version="1.0" encoding="UTF-8"'), name
    assert 'w:eastAsia="zh-TW"' in styles and 'w:eastAsia="en-US"' not in styles
    assert 'w:eastAsiaTheme' not in styles  # 不使用對應到日文、簡體字型的佈景主題字型
    assert '<w:font w:name="標楷體"><w:altName w:val="DFKai-SB"/>' in fonts and '<w:charset w:val="88"/>' in fonts
    assert "ＭＳ 明朝" not in fonts and "ＭＳ ゴシック" not in fonts


def test_new_document_is_zh_tw(tmp_path: Path) -> None:
    d = new_document()
    d.add_paragraph("毒駕防制「修法」…今年底完成")
    path = tmp_path / "a.docx"
    d.save(str(path))
    _check(path)


def test_transcript_docx_is_zh_tw(tmp_path: Path) -> None:
    doc = TranscriptDocument(source="t", folder=str(tmp_path),
                             segments=[RawSegment(start=0, end=3, speaker="講者1", text="大家好，開始開會。")])
    doc.info.title = "測試會議"
    _check(render_transcript(doc, tmp_path / "out"))
