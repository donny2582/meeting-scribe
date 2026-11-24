"""「產出文件」分頁（離屏測試）。"""

import os
from pathlib import Path
from typing import Any

import docx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox

from meeting_scribe import config as app_config
from meeting_scribe import tasks
from meeting_scribe.gui.main_window import MainWindow
from meeting_scribe.models import RawSegment, SttResult


@pytest.fixture
def win(monkeypatch: pytest.MonkeyPatch) -> Any:
    app = QApplication.instance() or QApplication([])
    asked: list[str] = []

    def dialog(*args: Any, **kwargs: Any) -> Any:
        asked.append(str(args[2]) if len(args) > 2 else "")
        return QMessageBox.StandardButton.Yes

    for kind in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, kind, dialog)
    store: dict[str, str] = {}
    w = MainWindow(secret_store={"get_secret": store.get, "set_secret": store.__setitem__,
                                 "delete_secret": lambda n: None})
    w.asked = asked  # type: ignore[attr-defined]
    yield w
    w.document._dirty = False
    w.close()
    del app


def _out(tmp_path: Path) -> tasks.TranscribeOutput:
    segs = [RawSegment(start=0, end=3, speaker="講者1", text="好，我們開始開會。"),
            RawSegment(start=4, end=9, speaker="講者2", text="請公路局說明進度。"),
            RawSegment(start=10, end=14, speaker="講者3", text="公路局報告。")]
    result = SttResult(engine="soniox", engine_version="Soniox stt-async-v6", audio_duration=5400,
                       elapsed_sec=60, locales=["zh-TW"], segments=segs)
    folder = tmp_path / "job"
    folder.mkdir()
    return tasks.TranscribeOutput(folder, folder / "stt_raw.json", folder / "逐字稿草稿.txt", result)


def test_empty_state(win: Any) -> None:
    page = win.document
    assert not page.produce_button.isEnabled() and not page.info_box.isEnabled()
    assert not page.empty_hint.isHidden()


def test_transcription_jumps_to_document_tab(win: Any, tmp_path: Path) -> None:
    audio = tmp_path / "會議.m4a"
    audio.write_bytes(b"x")
    win.open_transcription(_out(tmp_path), audio)
    page = win.document
    assert win.tabs.currentWidget() is page
    assert page.table.rowCount() == 3 and page.produce_button.isEnabled()
    assert page.end.time().toString("HH:mm") == "15:30"  # 14:00 ＋ 1.5 小時錄音
    assert page.doc.audio_name == "會議.m4a" and "轉錄結果" in page.source_label.text()
    assert "共 3 段、3 位講者" in page.count_label.text()


def test_edit_rename_merge_and_produce(win: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    win.open_transcription(_out(tmp_path), None)
    page = win.document
    page.table.item(1, 2).setText("請公路局說明本案進度。")  # 修正文字
    assert page.doc.segments[1].text == "請公路局說明本案進度。" and page.dirty

    answers = iter([("講者3", True)])
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: next(answers))
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("公路局", True))
    page.rename_speaker()
    assert page.table.item(2, 1).text() == "公路局"

    page.table.item(0, 1).setText("公路局")  # 改單一片段的講者：沿用既有講者
    assert page.doc.segments[0].speaker == "講者3"

    page.title.setPlainText("道安業務研商會議")
    page.location.setCurrentText("交通部第1會議室")
    page.chair.setCurrentText("吳司長")
    page.recorder.setText("王小明")
    page.type_buttons["agenda"].setChecked(True)
    page.agenda.setPlainText("一、科技執法\n\n二、容錯道路工程")
    page.want_mindmap.setChecked(True)
    page.produce()

    assert page.last_output is not None and page.last_output.exists()
    assert page.last_output.parent == tmp_path / "job" / "輸出"
    texts = [p.text for p in docx.Document(str(page.last_output)).paragraphs]
    assert "請公路局說明本案進度。" in texts
    assert any(t.startswith("[00:00:00] 公路局：") for t in texts)
    assert "心智圖" in page.result_label.text() and not page.dirty
    saved = (tmp_path / "job" / "document.json").read_text(encoding="utf-8")
    assert "交通部第1會議室" in saved and '"meeting_type": "agenda"' in saved
    ui = app_config.load_config()["ui"]
    assert ui["recent_locations"][0] == "交通部第1會議室" and ui["last_recorder"] == "王小明"


def test_produce_requires_title(win: Any, tmp_path: Path) -> None:
    win.open_transcription(_out(tmp_path), None)
    win.document.produce()
    assert win.document.last_output is None
    assert any("會議名稱" in a for a in win.asked)


def test_import_existing_transcript(win: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "已有的逐字稿.txt"
    src.write_text("[00:00:01] 主席：開會。\n[00:00:05] 講者2：報告。\n", encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(src), ""))
    win.document.import_file()
    page = win.document
    assert page.table.rowCount() == 2 and "已有的逐字稿.txt" in page.source_label.text()
    assert page.doc.folder.endswith("_已有的逐字稿_匯入")


def test_unsaved_changes_prompt_on_new_transcription(win: Any, tmp_path: Path) -> None:
    win.open_transcription(_out(tmp_path), None)
    win.document.title.setPlainText("改過")
    assert win.document.dirty
    second = tmp_path / "second"
    second.mkdir()
    out = _out(second)
    win.open_transcription(out, None)  # 對話框回答 Yes（≠ Save/Discard/Cancel）→ 視為不取消
    assert any("尚未儲存" in a for a in win.asked)
