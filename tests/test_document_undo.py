"""「產出文件」分頁的復原、重做。"""

import os
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from meeting_scribe import tasks
from meeting_scribe.gui.main_window import MainWindow
from meeting_scribe.models import RawSegment, SttResult
from meeting_scribe.transcript_io import EditHistory, TranscriptDocument


def _segments() -> list[RawSegment]:
    return [RawSegment(start=0, end=3, speaker="講者1", text="好，我們開始開會。"),
            RawSegment(start=4, end=9, speaker="講者2", text="請公路局說明進度。"),
            RawSegment(start=10, end=14, speaker="講者3", text="公路局報告。")]


def _state(doc: TranscriptDocument) -> tuple[list[tuple[str, str]], dict[str, str]]:
    return [(s.speaker, s.text) for s in doc.segments], dict(doc.speaker_names)


# ---- 核心 ----
def test_history_undo_redo_and_new_edit_clears_redo(tmp_path: Path) -> None:
    doc = TranscriptDocument(source="t", folder=str(tmp_path), segments=_segments())
    original = _state(doc)
    h = EditHistory()
    h.record(doc, "講者命名：講者3 → 公路局")
    doc.rename_speaker("講者3", "公路局")
    h.record(doc, "合併講者：講者2 併入 講者1")
    doc.merge_speaker("講者2", "講者1")
    after = _state(doc)
    assert h.undo_label == "合併講者：講者2 併入 講者1"
    assert h.undo(doc) == "合併講者：講者2 併入 講者1"
    assert [s.speaker for s in doc.segments] == ["講者1", "講者2", "講者3"]
    assert h.undo(doc) == "講者命名：講者3 → 公路局" and _state(doc) == original
    assert h.undo(doc) is None  # 沒有更多可以復原
    h.redo(doc)
    h.redo(doc)
    assert _state(doc) == after and h.redo(doc) is None
    h.undo(doc)
    h.record(doc, "修改內容")  # 新的修改會清掉「重做」
    assert h.redo_label is None


# ---- 介面 ----
@pytest.fixture
def page(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    w = MainWindow(secret_store={"get_secret": lambda n: None, "set_secret": lambda n, v: None,
                                 "delete_secret": lambda n: None})
    result = SttResult(engine="soniox", engine_version="v", audio_duration=60, elapsed_sec=1, locales=["zh-TW"],
                       segments=_segments())
    folder = tmp_path / "job"
    folder.mkdir()
    w.open_transcription(tasks.TranscribeOutput(folder, folder / "a.json", folder / "a.txt", result), None)
    p = w.document
    p.monkeypatch = monkeypatch  # type: ignore[attr-defined]
    yield p
    p._dirty = False
    w.close()
    del app


def _answers(monkeypatch: pytest.MonkeyPatch, items: list[str], texts: list[str]) -> None:
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: (items.pop(0), True))
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: (texts.pop(0), True))


def test_undo_redo_reset_in_gui(page: Any) -> None:
    def speakers() -> list[str]:
        return [page.table.item(r, 1).text() for r in range(page.table.rowCount())]

    assert not page.undo_button.isEnabled() and not page.redo_button.isEnabled()
    assert not hasattr(page, "reset_button")  # 不提供「重新來過」
    _answers(page.monkeypatch, ["講者3", "講者2", "講者1"], ["公路局"])
    page.rename_speaker()  # 講者3 → 公路局
    page.merge_speakers()  # 講者2 併入 講者1（操作錯誤）
    page.table.item(0, 2).setText("改錯的內容")
    assert speakers() == ["講者1", "講者1", "公路局"]
    assert "修改 00:00:00 的內容" in page.undo_button.toolTip()

    page.undo()  # 復原文字
    assert page.table.item(0, 2).text() == "好，我們開始開會。"
    page.undo()  # 復原合併
    assert speakers() == ["講者1", "講者2", "公路局"]
    assert page.redo_button.isEnabled() and "合併講者" in page.redo_button.toolTip()
    page.redo()
    assert speakers() == ["講者1", "講者1", "公路局"]
    assert "已重做：合併講者" in page.panel.log.toPlainText()
    page.undo()
    page.undo()  # 一路復原到最初
    assert speakers() == ["講者1", "講者2", "講者3"] and page.doc.speaker_names == {}
    assert not page.undo_button.isEnabled()
    assert page.dirty


def test_history_cleared_when_another_document_loaded(page: Any, tmp_path: Path) -> None:
    _answers(page.monkeypatch, ["講者1"], ["主席"])
    page.rename_speaker()
    assert page.undo_button.isEnabled()
    page._dirty = False
    page.load(TranscriptDocument(source="另一份", folder=str(tmp_path / "b"), segments=_segments()))
    assert not page.undo_button.isEnabled() and page.history.undo_label is None
