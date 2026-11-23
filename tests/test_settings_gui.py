"""設定視窗（離屏測試，不碰真實的 Windows 認證管理員與網路）。"""

import os
import tomllib
from pathlib import Path
from typing import Any

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from meeting_scribe import config as app_config
from meeting_scribe.gui import settings as gui_settings


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    for name in app_config.SECRET_NAMES:
        monkeypatch.delenv(f"MEETINGSCRIBE_{name.upper()}", raising=False)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    return {"openai_api_key": "sk-old-9999"}


def _window(store: dict[str, str]) -> gui_settings.SettingsWindow:
    return gui_settings.SettingsWindow(
        get_secret=store.get,
        set_secret=store.__setitem__,
        delete_secret=lambda n: store.pop(n, None),
    )


def test_save_keys_and_config(app: QApplication, store: dict[str, str], tmp_path: Path) -> None:
    win = _window(store)
    assert win.rows["openai_api_key"].status.text() == "****9999"
    assert win.rows["azure_speech_key"].status.text() == "（未設定）"

    win.rows["azure_speech_key"].edit.setText("  az-new-key-1234  ")
    win.azure_region.setCurrentText("eastasia")
    win.azure_price.setValue(1.5)
    assert win.save()

    assert store["azure_speech_key"] == "az-new-key-1234"
    assert store["openai_api_key"] == "sk-old-9999"  # 留空不變更
    assert win.rows["azure_speech_key"].edit.text() == ""
    assert win.rows["azure_speech_key"].status.text() == "****1234"
    with open(tmp_path / "MeetingScribe" / "config.toml", "rb") as f:
        saved = tomllib.load(f)
    assert saved["stt"]["azure"]["region"] == "eastasia"
    assert saved["stt"]["azure"]["price_per_hour"] == 1.5
    assert app_config.load_config()["stt"]["azure"]["region"] == "eastasia"
    win.close()


def test_clear_key(app: QApplication, store: dict[str, str]) -> None:
    win = _window(store)
    win.clear_key("openai_api_key")
    assert "openai_api_key" not in store
    assert win.rows["openai_api_key"].status.text() == "（未設定）"
    win.close()


def test_password_field_hidden(app: QApplication, store: dict[str, str]) -> None:
    from PySide6.QtWidgets import QLineEdit

    win = _window(store)
    row = win.rows["openai_api_key"]
    assert row.edit.echoMode() == QLineEdit.EchoMode.Password
    row.show.setChecked(True)
    assert row.edit.echoMode() == QLineEdit.EchoMode.Normal
    win.close()


@pytest.mark.parametrize("status,ok,text", [(200, True, "有效"), (401, False, "金鑰無效"), (404, False, "HTTP 404")])
def test_check_key(monkeypatch: pytest.MonkeyPatch, status: int, ok: bool, text: str) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_get(url: str, **kwargs: Any) -> httpx.Response:
        calls.append((url, kwargs))
        return httpx.Response(status, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    cfg = {"stt": {"azure": {"region": "eastasia", "endpoint": ""}}}
    result_ok, msg = gui_settings.check_key("azure_speech_key", "k", cfg)
    assert result_ok is ok and text in msg
    assert calls[0][0].startswith("https://eastasia.api.cognitive.microsoft.com/")


def test_check_key_needs_region() -> None:
    ok, msg = gui_settings.check_key("azure_speech_key", "k", {"stt": {"azure": {}}})
    assert not ok and "區域" in msg
