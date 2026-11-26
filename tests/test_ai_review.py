"""AI 校正的比對：時間碼對齊（容錯）、淡橘／淡黃／淡綠標記、逐列還原講者、輸出與比對 txt、選擇校正方式。"""

import os
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from meeting_scribe import tasks
from meeting_scribe.models import RawSegment
from meeting_scribe.postprocess import ai_review
from meeting_scribe.postprocess.semantic import RoleSuggestion, SegmentReassign, Suggestions, TextFix
from meeting_scribe.transcript_io import TranscriptDocument, import_transcript

DRAFT = """[00:00:05] 講者1：好，我們開始開會，今天討論獨架防制。
[00:00:20] 講者1：公路局報告，修法預計11月完成。
[00:00:40] 講者2：了解，請監理司補充。
[00:01:10] 講者3：監理司這邊沒有意見。
"""
# AI 校正檔：時間碼差 1 秒（容錯內）、第 3 列沒有講者、第 4 列時間差太多（找不到對應）
CORRECTED = """[00:00:06] 主席：好，我們開始開會，今天討論毒駕防制。
[00:00:19] 公路局：公路局報告，修法預計11月完成。
[00:00:41] 了解，請監理司補充。
[00:01:30] 監理司：監理司這邊沒有意見。
"""


def _seg(start: float, speaker: str, text: str) -> RawSegment:
    return RawSegment(start=start, end=start + 3, speaker=speaker, text=text)


# ---- 對齊 ----
def test_align_left_join_with_tolerance() -> None:
    base = [_seg(5, "講者1", "a"), _seg(20, "講者1", "b"), _seg(40, "講者2", "c"), _seg(70, "講者3", "d")]
    corr = [_seg(6, "主席", "a2"), _seg(19, "公路局", "b"), _seg(41, "講者", "c2"), _seg(90, "監理司", "d")]
    joined, unmatched = ai_review.align(base, corr)
    assert [(s.speaker, s.text) for s in joined] == [("主席", "a2"), ("公路局", "b"), ("講者2", "c2"), ("講者3", "d")]
    assert [s.start for s in joined] == [5, 20, 40, 70]  # 以草稿為主，時間碼不變
    assert unmatched == [2, 3]  # 沒有講者、找不到對應 → 淡黃色


def test_align_picks_nearest_and_uses_each_row_once() -> None:
    base = [_seg(10, "講者1", "a"), _seg(11, "講者2", "b")]
    corr = [_seg(11.2, "乙", "b"), _seg(10.1, "甲", "a")]
    joined, unmatched = ai_review.align(base, corr)
    assert [s.speaker for s in joined] == ["甲", "乙"] and unmatched == []
    joined, unmatched = ai_review.align(base, [_seg(0, "甲", "x")], timed=False)  # 沒有時間碼 → 依順序
    assert [s.speaker for s in joined] == ["甲", "講者2"] and unmatched == [1]


def test_load_files(tmp_path: Path) -> None:
    (tmp_path / "逐字稿草稿.txt").write_text(DRAFT, encoding="utf-8")
    (tmp_path / "逐字稿草稿_AI校正.txt").write_text(CORRECTED, encoding="utf-8")
    loaded = ai_review.load_files(tmp_path / "逐字稿草稿.txt", tmp_path / "逐字稿草稿_AI校正.txt", tmp_path / "work")
    assert len(loaded.base) == len(loaded.joined) == 4 and loaded.timed
    assert loaded.joined[0].speaker == "主席" and "毒駕" in loaded.joined[0].text
    assert loaded.unmatched == [2, 3]


# ---- 標記與還原講者 ----
def test_review_marks_and_restore_toggle() -> None:
    base = [_seg(5, "講者1", "獨架"), _seg(20, "講者1", "公路局報告")]
    doc = TranscriptDocument(source="t", segments=[_seg(5, "主席", "毒駕"), _seg(20, "公路局", "公路局報告")])
    ai_review.start_review(doc, base, "雲端比對")
    assert ai_review.speaker_changed(doc, 0) and ai_review.text_changed(doc, 0)
    assert ai_review.speaker_changed(doc, 1) and not ai_review.text_changed(doc, 1)
    assert ai_review.can_restore(doc, 1)
    assert ai_review.set_restored(doc, 1, True)  # 勾選：回到草稿的講者
    assert doc.segments[1].speaker == "講者1" and not ai_review.speaker_changed(doc, 1)
    assert ai_review.can_restore(doc, 1)  # 仍可以取消勾選
    ai_review.set_restored(doc, 1, False)  # 取消勾選：回到 AI 校正後
    assert doc.segments[1].speaker == "公路局" and doc.ai_review.restored == []  # type: ignore[union-attr]
    loaded = TranscriptDocument.model_validate_json(doc.model_dump_json())  # 比對狀態會跟著文件存檔
    assert loaded.ai_review is not None and loaded.ai_review.baseline[0].text == "獨架"


def test_write_txt_roundtrip(tmp_path: Path) -> None:
    segs = [_seg(5, "主席", "好，開會。"), _seg(65, "公路局", "報告。")]
    path = ai_review.write_txt(segs, tmp_path / "逐字稿草稿_AI校正.txt", "研商會", "逐字稿草稿（AI 校正後）")
    again = import_transcript(path, tmp_path / "x")
    assert [(s.start, s.speaker, s.text) for s in again.segments] == [(5, "主席", "好，開會。"), (65, "公路局", "報告。")]


# ---- 介面 ----
@pytest.fixture
def page(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    from PySide6.QtWidgets import QApplication, QMessageBox

    from meeting_scribe.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    w = MainWindow(secret_store={"get_secret": lambda n: None, "set_secret": lambda n, v: None,
                                 "delete_secret": lambda n: None})
    doc = TranscriptDocument(source="t", folder=str(tmp_path / "job"), segments=[
        _seg(5, "講者1", "好，我們開始開會，今天討論獨架防制。"), _seg(20, "講者1", "公路局報告，修法預計11月完成。"),
        _seg(40, "講者2", "了解，請監理司補充。"), _seg(70, "講者3", "監理司這邊沒有意見。")])
    doc.info.title = "道安研商會議"
    w.document.load(doc)
    w.document.monkeypatch = monkeypatch  # type: ignore[attr-defined]
    yield w.document
    w.document._dirty = False
    w.close()
    del app


def _wait(page: Any) -> None:
    from PySide6.QtTest import QTest

    for _ in range(200):
        QTest.qWait(10)
        if not page.panel.running:
            return


def _bg(page: Any, row: int, col: int) -> str:
    return page.table.item(row, col).background().color().name()


def test_cloud_correction_marks_writes_files_and_restore(page: Any) -> None:
    from PySide6.QtCore import Qt

    s = Suggestions(fixes=[TextFix(0, page.doc.segments[0].text, "好，我們開始開會，今天討論毒駕防制。", "同音", "AI")],
                    roles=[RoleSuggestion("講者1", "主席", "高", [0], "主持")],
                    reassigns=[SegmentReassign(1, "講者1", "講者3", "高", "自稱公路局")])
    mp = page.monkeypatch
    mp.setattr(page, "_choose_correction_source", lambda: ("cloud", None, None))
    mp.setattr(tasks, "suggest_corrections", lambda *a, **k: s)
    mp.setattr(page, "_open_correction_dialog", lambda x: (x.fixes, x.roles, [], x.reassigns))
    page.ai_correct()
    _wait(page)
    names = [page.table.item(r, 1).text() for r in range(4)]
    assert names == ["主席", "講者3", "講者2", "講者3"]
    assert _bg(page, 0, 1) == "#ffe0b2" and _bg(page, 0, 2) == "#ffe0b2"  # 講者、內容都改了 → 淡橘色
    assert _bg(page, 1, 1) == "#ffe0b2" and _bg(page, 2, 1) != "#ffe0b2"  # 沒改的列不標記
    folder = page.output_folder()
    assert (folder / "逐字稿草稿_AI校正.txt").read_text(encoding="utf-8-sig").count("主席：") == 1
    assert "獨架" in (folder / "逐字稿草稿.txt").read_text(encoding="utf-8-sig")

    restore = page.table.item(1, 3)
    assert restore.flags() & Qt.ItemFlag.ItemIsUserCheckable  # AI 改過講者的列可以勾
    assert not page.table.item(2, 3).flags() & Qt.ItemFlag.ItemIsUserCheckable
    restore.setCheckState(Qt.CheckState.Checked)  # 還原講者
    assert page.table.item(1, 1).text() == "講者1" and _bg(page, 1, 1) != "#ffe0b2"  # 回到原本的顏色
    assert page.table.item(1, 3).checkState() == Qt.CheckState.Checked
    page.table.item(1, 3).setCheckState(Qt.CheckState.Unchecked)  # 再取消：回到 AI 校正後
    assert page.table.item(1, 1).text() == "講者3" and _bg(page, 1, 1) == "#ffe0b2"
    page.undo()  # 復原「取消還原」
    assert page.table.item(1, 1).text() == "講者1"
    page.undo()
    page.undo()  # 復原整個 AI 校正：標記也消失
    assert page.doc.ai_review is None and _bg(page, 0, 1) != "#ffe0b2"


def test_files_correction_left_join_and_green(page: Any, tmp_path: Path) -> None:
    (tmp_path / "逐字稿草稿.txt").write_text(DRAFT, encoding="utf-8")
    (tmp_path / "逐字稿草稿_AI校正.txt").write_text(CORRECTED, encoding="utf-8")
    page.monkeypatch.setattr(page, "_choose_correction_source",
                             lambda: ("files", tmp_path / "逐字稿草稿.txt", tmp_path / "逐字稿草稿_AI校正.txt"))
    page.ai_correct()
    assert [page.table.item(r, 1).text() for r in range(4)] == ["主席", "公路局", "講者2", "講者3"]
    assert _bg(page, 0, 1) == "#ffe0b2" and _bg(page, 0, 2) == "#ffe0b2"
    assert all(_bg(page, r, c) == "#fff9c4" for r in (2, 3) for c in range(3))  # 找不到對應或沒有講者 → 淡黃色
    assert "2 列找不到對應或沒有講者" in page.panel.log.toPlainText()
    page.undo()
    assert page.doc.ai_review is None and page.table.item(0, 1).text() == "講者1"


def test_source_dialog(page: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from meeting_scribe.gui.correction_dialog import CorrectionSourceDialog

    folder = tmp_path / "out"
    folder.mkdir()
    (folder / "逐字稿草稿.txt").write_text(DRAFT, encoding="utf-8")
    d = CorrectionSourceDialog(page, folder)
    assert d.mode() == "cloud" and not d.file_box.isEnabled()
    d.files.setChecked(True)
    assert d.file_box.isEnabled() and d.paths()[0] == folder / "逐字稿草稿.txt"  # 已有的檔案自動帶入
    asked: list[str] = []
    monkeypatch.setattr(d._message, "information", lambda *a, **k: asked.append(a[2]))
    d._accept()  # 還沒選校正檔 → 提醒，不關閉
    assert asked and "AI 校正檔" in asked[0] and d.result() == 0
    (folder / "逐字稿草稿_AI校正.txt").write_text(CORRECTED, encoding="utf-8")
    d.corrected.set_path(folder / "逐字稿草稿_AI校正.txt")
    d._accept()
    assert d.result() == 1 and d.mode() == "files"


def test_transcribed_document_writes_into_transcription_folder(page: Any, tmp_path: Path) -> None:
    """從「轉錄」跳轉來的文件：AI 校正檔寫在轉錄的工作資料夾，和轉錄產生的逐字稿草稿.txt 放一起，不覆蓋草稿。"""
    from meeting_scribe.gui.correction_dialog import CorrectionSourceDialog

    job = Path(page.doc.folder)
    job.mkdir(parents=True, exist_ok=True)
    (job / "逐字稿草稿.txt").write_text("辨識引擎：Soniox\n\n" + DRAFT, encoding="utf-8-sig")  # 轉錄時產生的草稿
    assert page._review_folder() == (job, True)
    s = Suggestions(fixes=[TextFix(0, page.doc.segments[0].text, "好，我們開始開會，今天討論毒駕防制。", "同音", "AI")])
    mp = page.monkeypatch
    mp.setattr(page, "_choose_correction_source", lambda: ("cloud", None, None))
    mp.setattr(tasks, "suggest_corrections", lambda *a, **k: s)
    mp.setattr(page, "_open_correction_dialog", lambda x: (x.fixes, [], [], []))
    page.ai_correct()
    _wait(page)
    assert "毒駕" in (job / "逐字稿草稿_AI校正.txt").read_text(encoding="utf-8-sig")
    assert (job / "逐字稿草稿.txt").read_text(encoding="utf-8-sig").startswith("辨識引擎：Soniox")  # 草稿沒被覆蓋
    assert not (page.output_folder() / "逐字稿草稿_AI校正.txt").exists()
    d = CorrectionSourceDialog(page, page._review_folder()[0])  # 「已有現成校正檔」自動帶入同一個資料夾的兩個檔案
    assert d.paths() == (job / "逐字稿草稿.txt", job / "逐字稿草稿_AI校正.txt")

def test_open_draft_folder_button(page: Any) -> None:
    opened: list[Path] = []
    page.monkeypatch.setattr("meeting_scribe.gui.document_page.open_path", lambda p: opened.append(Path(p)))
    assert page.draft_folder_button.isEnabled()
    page.draft_folder_button.click()  # 匯入的逐字稿（沒有轉錄草稿）→ 會議輸出資料夾
    job = Path(page.doc.folder)
    job.mkdir(parents=True, exist_ok=True)
    (job / "逐字稿草稿.txt").write_text(DRAFT, encoding="utf-8")
    page.draft_folder_button.click()  # 轉錄來的逐字稿 → 轉錄的工作資料夾
    assert opened == [page.output_folder(), job]

def test_manual_edits_are_light_green(page: Any, tmp_path: Path) -> None:
    """手動修改的講者或內容儲存格標淡綠色（優先於 AI 的淡橘色）；復原會一併取消，標記隨文件儲存。"""
    page.table.item(1, 2).setText("公路局報告，修法預計今年11月完成。")  # 手動改內容
    assert _bg(page, 1, 2) == "#dcedc8" and _bg(page, 1, 1) != "#dcedc8"  # 只標改過的那一格
    page.table.item(2, 1).setText("主席")  # 手動改講者
    assert _bg(page, 2, 1) == "#dcedc8" and _bg(page, 2, 2) != "#dcedc8"
    assert "手動修改過" in page.table.item(2, 1).toolTip()
    page.undo()  # 復原講者修改 → 標記也取消
    assert _bg(page, 2, 1) != "#dcedc8" and _bg(page, 1, 2) == "#dcedc8"
    loaded = TranscriptDocument.load(page.doc.save())
    assert loaded.manual_cells == [(1, 2)]

    s = Suggestions(fixes=[TextFix(0, page.doc.segments[0].text, "好，我們開始開會，今天討論毒駕防制。", "同音", "AI")])
    mp = page.monkeypatch
    mp.setattr(page, "_choose_correction_source", lambda: ("cloud", None, None))
    mp.setattr(tasks, "suggest_corrections", lambda *a, **k: s)
    mp.setattr(page, "_open_correction_dialog", lambda x: (x.fixes, [], [], []))
    page.ai_correct()
    _wait(page)
    assert _bg(page, 0, 2) == "#ffe0b2"  # AI 改的＝淡橘色
    page.table.item(0, 2).setText("好，我們開始開會，今天討論毒駕防制案。")  # 再手動改 → 淡綠色優先
    assert _bg(page, 0, 2) == "#dcedc8"