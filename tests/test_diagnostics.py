"""錯誤安全網：日誌輪替、錯誤分類與記錄、設定檔格式檢查、問題回報檔、全域錯誤攔截。"""

import logging
import os
import re
import sys
import threading
import tomllib
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from meeting_scribe import __version__, diagnostics
from meeting_scribe.config import user_config_path
from meeting_scribe.stt.base import SttError


@pytest.fixture(autouse=True)
def _restore_global_state() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    hooks = (sys.excepthook, threading.excepthook)
    yield
    for h in root.handlers:
        if h not in handlers:
            h.close()
    root.handlers[:] = handlers
    root.setLevel(level)
    sys.excepthook, threading.excepthook = hooks


def _log_text() -> str:
    for h in logging.getLogger().handlers:
        h.flush()
    return (diagnostics.log_dir() / diagnostics.LOG_FILE).read_text(encoding="utf-8")


def test_version_matches_pyproject() -> None:
    root = Path(__file__).resolve().parents[1]
    with open(root / "pyproject.toml", "rb") as f:
        assert tomllib.load(f)["project"]["version"] == __version__


def test_logging_rotates_and_writes_startup_line() -> None:
    diagnostics.setup_logging(console=False)
    handler = next(h for h in logging.getLogger().handlers if isinstance(h, logging.handlers.RotatingFileHandler))
    assert handler.maxBytes == diagnostics.LOG_MAX_BYTES and handler.backupCount == diagnostics.LOG_BACKUPS
    assert f"MeetingScribe {__version__} 啟動" in _log_text()


def test_expected_and_unexpected_errors_are_described_and_logged() -> None:
    diagnostics.setup_logging(console=False)
    known = SttError("Soniox 帳戶餘額不足（HTTP 402）")
    assert diagnostics.describe(known) == "Soniox 帳戶餘額不足（HTTP 402）"
    try:
        {}["title"]
    except KeyError as exc:
        bug = exc
    message = diagnostics.describe(bug)
    assert "未預期的錯誤（KeyError" in message and "回報問題" in message
    diagnostics.log_exception(known, "轉錄")
    diagnostics.log_exception(bug, "產出文件")
    text = _log_text()
    assert "轉錄：Soniox 帳戶餘額不足" in text
    assert "產出文件：未預期的錯誤" in text and "Traceback" in text and "test_diagnostics.py" in text


def test_bad_user_config_is_explained_and_can_be_backed_up() -> None:
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[general]\noutput_dir = "D:\\會議"\n', encoding="utf-8")
    problem = diagnostics.check_user_config()
    assert problem and "第 2 行" in problem and "反斜線" in problem
    backup = diagnostics.backup_user_config()
    assert backup.exists() and not path.exists() and "格式錯誤" in backup.name
    assert diagnostics.check_user_config() is None


def test_report_zip_excludes_names_and_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    diagnostics.setup_logging(console=False)
    logging.getLogger("meeting_scribe").info("轉錄完成：12 個片段")
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[stt]\ndefault_engine = "soniox"\n\n[ui]\nlast_recorder = "王小明"\n'
                    'recent_chairs = ["陳次長"]\n', encoding="utf-8")
    monkeypatch.setattr("meeting_scribe.config.get_secret", lambda n: "sk-ant-SECRET" if "anthropic" in n else None)
    report = diagnostics.make_report(tmp_path / "回報")
    with zipfile.ZipFile(report) as z:
        names = z.namelist()
        everything = "".join(z.read(n).decode("utf-8", errors="replace") for n in names)
    assert "版本與系統資訊.txt" in names and any(n.startswith("logs/") for n in names)
    assert __version__ in everything and "soniox" in everything and "轉錄完成" in everything
    assert "Anthropic API 金鑰：已設定" in everything
    assert "王小明" not in everything and "陳次長" not in everything and "SECRET" not in everything


def test_global_hook_logs_and_shows_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

    from meeting_scribe.gui import error_dialog

    app = QApplication.instance() or QApplication([])
    diagnostics.setup_logging(console=False)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    reporter = error_dialog.install()
    reporter.shown.clear()
    button = QPushButton()

    def boom() -> None:
        raise ValueError("出錯了")

    button.clicked.connect(boom)
    button.click()  # 按鈕處理函式發生錯誤（Qt 會交給 sys.excepthook）
    t = threading.Thread(target=lambda: {}["x"], name="測試執行緒")
    t.start()
    t.join()
    for _ in range(20):
        QCoreApplication.processEvents()
    text = _log_text()
    assert "未預期的錯誤" in text and "ValueError: 出錯了" in text and "KeyError" in text
    assert any("ValueError" in m for m in reporter.shown) and any("背景工作（測試執行緒）" in m for m in reporter.shown)
    del app


def test_run_panel_unexpected_error_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from meeting_scribe.gui.widgets import RunPanel

    app = QApplication.instance() or QApplication([])
    diagnostics.setup_logging(console=False)
    panel = RunPanel()

    def work(progress: Any, cancel: Any) -> Any:
        return {}["title"]

    panel.start("產出文件", work, lambda r: None)
    for _ in range(100):
        QTest.qWait(10)
        if not panel.running:
            break
    assert re.search(r"失敗：程式發生未預期的錯誤（KeyError", panel.status.text())
    assert "產出文件：未預期的錯誤" in _log_text()
    del app
