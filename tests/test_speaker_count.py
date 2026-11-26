"""「轉錄」分頁的預估講者人數：只有 Azure 可以指定；Soniox、OpenAI 鎖定為「自動」，也不會送出。"""

import os
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from meeting_scribe import tasks
from meeting_scribe.gui.main_window import MainWindow


@pytest.fixture
def page() -> Any:
    app = QApplication.instance() or QApplication([])
    w = MainWindow(secret_store={"get_secret": lambda n: None, "set_secret": lambda n, v: None,
                                 "delete_secret": lambda n: None})
    yield w.transcribe
    w.close()
    del app


def _select(page: Any, engine: str) -> None:
    page.engine.setCurrentIndex(page.engine.findData(engine))


def test_speaker_count_locked_for_soniox_and_openai(page: Any) -> None:
    _select(page, "azure")
    assert page.speakers.isEnabled() and page.speakers_note.text() == ""
    page.speakers.setValue(5)
    _select(page, "soniox")
    assert not page.speakers.isEnabled() and page.speakers.text() == "自動"
    assert "自動判斷講者人數" in page.speakers_note.text()
    _select(page, "openai")
    assert not page.speakers.isEnabled() and page.speakers.value() == 0
    _select(page, "azure")
    assert page.speakers.isEnabled()


def test_soniox_never_sends_speaker_count(page: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    page.audio.set_path(audio)
    _select(page, "soniox")
    page.speakers.setValue(4)  # 即使程式裡被設定成 4（介面上已鎖定），送出時仍是自動
    sent: list[Any] = []
    monkeypatch.setattr(page.host, "prepare_run", lambda engines: page.host.cfg)
    monkeypatch.setattr(tasks, "run_transcribe_job",
                        lambda engine, audio, langs, gl, cfg, reg, out, speakers, *a, **k: sent.append(speakers)
                        or (_ for _ in ()).throw(tasks.Cancelled("測試")))
    page.start()
    from PySide6.QtTest import QTest

    for _ in range(100):
        QTest.qWait(10)
        if not page.panel.running:
            break
    assert sent == [None]
