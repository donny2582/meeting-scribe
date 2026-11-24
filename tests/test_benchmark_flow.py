"""引擎評估：不必另外選擇標準答案，自動使用校正後的草稿。"""

import os
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from meeting_scribe import tasks
from meeting_scribe.benchmark import load_reference
from meeting_scribe.config import load_config
from meeting_scribe.models import RawSegment, SttResult
from meeting_scribe.postprocess.lang_detect import LanguageRegistry


def _fake_transcribe(monkeypatch: pytest.MonkeyPatch, text: str = "我們開始開會") -> None:
    def fake(cache: Path, reuse: bool, progress: Any = None, **kwargs: Any) -> SttResult:
        return SttResult(engine="soniox", engine_version="v", audio_duration=10, elapsed_sec=1, locales=["zh-TW"],
                         segments=[RawSegment(start=0, end=2, speaker="講者1", text=text)])

    monkeypatch.setattr(tasks, "load_or_transcribe", fake)


def _make(tmp_path: Path) -> Path:
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    return tasks.make_reference(audio, "soniox", ["zh"], [], load_config(), LanguageRegistry.load(),
                                tmp_path / "eval" / tasks.REFERENCE_FILE)


def test_reference_status_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_transcribe(monkeypatch)
    path = tmp_path / "eval" / tasks.REFERENCE_FILE
    assert tasks.reference_status(path) == "missing"
    _make(tmp_path)
    assert tasks.reference_status(path) == "uncorrected"
    path.write_text(path.read_text(encoding="utf-8-sig").replace("開始開會", "開始開會了"), encoding="utf-8-sig")
    assert tasks.reference_status(path) == "corrected"


def test_regenerate_backs_up_corrected_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_transcribe(monkeypatch)
    path = _make(tmp_path)
    path.write_text("start,end,speaker,lang,text\n0,2,甲,zh,我校正過的內容\n", encoding="utf-8-sig")
    _make(tmp_path)
    backups = list(path.parent.glob("reference_draft_備份_*.csv"))
    assert len(backups) == 1 and "我校正過的內容" in backups[0].read_text(encoding="utf-8-sig")
    assert tasks.reference_status(path) == "uncorrected"  # 新的草稿


def test_uncorrected_draft_not_backed_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_transcribe(monkeypatch)
    path = _make(tmp_path)
    _make(tmp_path)
    assert not list(path.parent.glob("*_備份_*.csv"))


def test_load_reference_big5_from_excel(tmp_path: Path) -> None:
    path = tmp_path / "ref.csv"
    path.write_bytes("start,end,speaker,lang,text\n00:00:01,00:00:03,主席,zh,開始開會\n".encode("cp950"))
    ref = load_reference(path)
    assert ref[0].text == "開始開會" and ref[0].speaker == "主席"


def test_report_warns_when_uncorrected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_transcribe(monkeypatch)
    path = _make(tmp_path)
    out = tasks.run_benchmark(tmp_path / "a.m4a", path, ["soniox"], ["zh"], [], load_config(),
                              LanguageRegistry.load(), path.parent)
    assert out.markdown.startswith("> ⚠ 標準答案（reference_draft.csv）尚未校正")


# ---- 介面 ----
@pytest.fixture
def page(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    from meeting_scribe.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    asked: list[str] = []
    answers = {"value": QMessageBox.StandardButton.Yes}

    def dialog(*a: Any, **k: Any) -> Any:
        asked.append(f"{a[1]}｜{a[2]}" if len(a) > 2 else "")
        return answers["value"]

    for kind in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, kind, dialog)
    w = MainWindow(secret_store={"get_secret": lambda n: None, "set_secret": lambda n, v: None,
                                 "delete_secret": lambda n: None})
    monkeypatch.setattr(w, "prepare_run", lambda engines: w.cfg)
    p = w.benchmark
    p.out_dir.set_path(tmp_path / "eval")
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    p.audio.set_path(audio)
    p.asked, p.answers, p.win = asked, answers, w  # type: ignore[attr-defined]
    yield p
    w.close()
    del app


def test_no_step_to_choose_reference(page: Any) -> None:
    assert not hasattr(page, "reference") and not hasattr(page, "lang_reference")
    assert "尚未產生" in page.ref_status.text() and not page.open_drafts.isEnabled()
    page.start()
    assert any("產生標準答案草稿" in a for a in page.asked)


def test_start_uses_reference_automatically(page: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_transcribe(monkeypatch)
    _make(tmp_path)
    page.refresh_status()
    assert "草稿尚未校正" in page.ref_status.text() and page.open_drafts.isEnabled()
    calls: list[Path] = []
    monkeypatch.setattr(tasks, "run_benchmark", lambda audio, ref, *a, **k: calls.append(ref)
                        or tasks.BenchmarkOutput(None, "", {}))
    page.answers["value"] = QMessageBox.StandardButton.No  # 不要用未校正的草稿
    page.start()
    assert calls == [] and any("尚未校正" in a for a in page.asked)

    ref = tmp_path / "eval" / tasks.REFERENCE_FILE
    ref.write_text(ref.read_text(encoding="utf-8-sig") + "2,4,甲,zh,補充\n", encoding="utf-8-sig")
    page.refresh_status()
    assert "已校正" in page.ref_status.text()
    page.start()
    for _ in range(100):
        QCoreApplication.processEvents()
        if not page.panel.running:
            break
        QTest.qWait(10)
    assert calls == [ref]  # 自動使用評估資料夾裡校正後的檔案
