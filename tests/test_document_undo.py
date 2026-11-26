"""「產出文件」分頁的復原與還原原本內容。"""

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

ORIGINAL = [("講者1", "好，我們開始開會。"), ("講者2", "請公路局說明進度。"), ("講者3", "公路局報告。")]


def _segments() -> list[RawSegment]:
    return [RawSegment(start=i * 5, end=i * 5 + 3, speaker=s, text=t) for i, (s, t) in enumerate(ORIGINAL)]


def _state(doc: TranscriptDocument) -> tuple[list[tuple[str, str]], dict[str, str]]:
    return [(s.speaker, s.text) for s in doc.segments], dict(doc.speaker_names)


# ---- 核心 ----
def test_history_undo(tmp_path: Path) -> None:
    doc = TranscriptDocument(source="t", folder=str(tmp_path), segments=_segments())
    original = _state(doc)
    h = EditHistory()
    h.record(doc, "講者命名：講者3 → 公路局")
    doc.rename_speaker("講者3", "公路局")
    h.record(doc, "合併講者：講者2 併入 講者1")
    doc.merge_speaker("講者2", "講者1")
    assert h.undo_label == "合併講者：講者2 併入 講者1"
    assert h.undo(doc) == "合併講者：講者2 併入 講者1"
    assert [s.speaker for s in doc.segments] == ["講者1", "講者2", "講者3"]
    assert h.undo(doc) == "講者命名：講者3 → 公路局" and _state(doc) == original
    assert h.undo(doc) is None  # 沒有更多可以復原


def test_original_is_kept_and_saved(tmp_path: Path) -> None:
    doc = TranscriptDocument(source="t", folder=str(tmp_path), segments=_segments())
    doc.rename_speaker("講者1", "主席")
    doc.segments[0] = doc.segments[0].model_copy(update={"text": "改過的內容"})
    loaded = TranscriptDocument.load(doc.save())  # 存檔後再開啟，仍記得原本的內容
    assert loaded.has_original() and loaded.restore_original()
    assert _state(loaded) == (ORIGINAL, {})


def test_old_document_without_original() -> None:
    """舊版存的文件（已修改、沒有原本內容）不會把目前內容誤當成原本的逐字稿。"""
    doc = TranscriptDocument.model_validate({"source": "t", "segments": [s.model_dump() for s in _segments()],
                                             "speaker_names": {"講者1": "主席"}, "edited": True})
    assert not doc.has_original() and not doc.restore_original()


# ---- 介面 ----
@pytest.fixture
def page(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    app = QApplication.instance() or QApplication([])
    answers = {"value": QMessageBox.StandardButton.Yes}
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: answers["value"])
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    w = MainWindow(secret_store={"get_secret": lambda n: None, "set_secret": lambda n, v: None,
                                 "delete_secret": lambda n: None})
    result = SttResult(engine="soniox", engine_version="v", audio_duration=60, elapsed_sec=1, locales=["zh-TW"],
                       segments=_segments())
    folder = tmp_path / "job"
    folder.mkdir()
    w.open_transcription(tasks.TranscribeOutput(folder, folder / "a.json", folder / "a.txt", result), None)
    p = w.document
    p.monkeypatch, p.answers = monkeypatch, answers  # type: ignore[attr-defined]
    yield p
    p._dirty = False
    w.close()
    del app


def _answers(monkeypatch: pytest.MonkeyPatch, items: list[str], texts: list[str]) -> None:
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: (items.pop(0), True))
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: (texts.pop(0), True))


def _speakers(page: Any) -> list[str]:
    return [page.table.item(r, 1).text() for r in range(page.table.rowCount())]


def test_undo_and_restore_original_in_gui(page: Any) -> None:
    assert not page.undo_button.isEnabled() and page.restore_button.isEnabled()
    assert not hasattr(page, "redo_button")  # 不再有「重做」
    _answers(page.monkeypatch, ["講者3", "講者2", "講者1"], ["公路局"])
    page.rename_speaker()  # 講者3 → 公路局
    page.merge_speakers()  # 講者2 併入 講者1（操作錯誤）
    page.table.item(0, 2).setText("改錯的內容")
    assert _speakers(page) == ["講者1", "講者1", "公路局"]
    assert "修改 00:00:00 的內容" in page.undo_button.toolTip()

    page.undo()  # 復原文字
    assert page.table.item(0, 2).text() == "好，我們開始開會。"
    page.undo()  # 復原合併
    assert _speakers(page) == ["講者1", "講者2", "公路局"]

    page.answers["value"] = QMessageBox.StandardButton.No
    page.restore_original()  # 確認視窗按「否」→ 不變
    assert _speakers(page) == ["講者1", "講者2", "公路局"]
    page.answers["value"] = QMessageBox.StandardButton.Yes
    page.restore_original()  # 還原原本內容
    assert _speakers(page) == ["講者1", "講者2", "講者3"] and page.doc.speaker_names == {}
    assert "已還原：逐字稿回到原本的內容" in page.panel.log.toPlainText()
    page.undo()  # 反悔：回到還原之前
    assert _speakers(page) == ["講者1", "講者2", "公路局"] and page.dirty


def test_restore_after_save_and_reopen(page: Any, tmp_path: Path) -> None:
    _answers(page.monkeypatch, ["講者1"], ["主席"])
    page.rename_speaker()
    page.table.item(1, 2).setText("改過的內容")
    path = page.doc.save()
    page._dirty = False
    page.load(TranscriptDocument.load(path))  # 關閉後重新開啟
    assert not page.undo_button.isEnabled()  # 復原紀錄不跨開啟
    page.restore_original()
    assert _speakers(page) == ["講者1", "講者2", "講者3"]
    assert page.table.item(1, 2).text() == "請公路局說明進度。"


def test_old_document_restores_to_opened_state(page: Any, tmp_path: Path) -> None:
    old = TranscriptDocument.model_validate({"source": "舊版", "folder": str(tmp_path / "old"),
                                             "segments": [s.model_dump() for s in _segments()],
                                             "speaker_names": {"講者1": "主席"}, "edited": True})
    page._dirty = False
    page.load(old)
    _answers(page.monkeypatch, ["主席", "講者2"], [])
    page.merge_speakers()
    page.restore_original()
    assert _speakers(page) == ["主席", "講者2", "講者3"]  # 回到這次開啟時的內容