"""所有測試共用：把設定與工作資料夾指到暫存目錄，不讀寫使用者真正的設定檔，也不碰 Windows 認證管理員。"""

from pathlib import Path

import pytest

from meeting_scribe import config as app_config


@pytest.fixture(autouse=True)
def _isolate_user_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(app_config, "documents_dir", lambda: tmp_path / "documents")
    for name in app_config.SECRET_NAMES:
        monkeypatch.delenv(f"MEETINGSCRIBE_{name.upper()}", raising=False)

    def _no_keyring(name: str) -> None:
        return None

    monkeypatch.setattr(app_config, "get_secret", _no_keyring)
    monkeypatch.setattr("meeting_scribe.tasks.get_secret", _no_keyring)
