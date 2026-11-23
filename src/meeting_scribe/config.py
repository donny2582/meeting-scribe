"""設定讀寫與 API 金鑰保存。

- 出廠值：``assets/default_config.toml``
- 使用者設定：``%APPDATA%\\MeetingScribe\\config.toml``（只需寫入要覆寫的項目）
- API 金鑰：Windows 認證管理員（keyring）；開發時可用環境變數 ``MEETINGSCRIBE_<名稱大寫>`` 覆寫。
"""

from __future__ import annotations

import copy
import os
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any

APP_NAME = "MeetingScribe"
KEYRING_SERVICE = "MeetingScribe"

#: 可設定的金鑰名稱與說明
SECRET_NAMES: dict[str, str] = {
    "azure_speech_key": "Azure AI Speech 金鑰",
    "openai_api_key": "OpenAI API 金鑰",
    "anthropic_api_key": "Anthropic API 金鑰",
}


def app_root() -> Path:
    """回傳程式根目錄（開發模式為專案根目錄，打包後為 PyInstaller 的資源目錄）。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[2]


def assets_dir() -> Path:
    """回傳 assets 目錄。"""
    return app_root() / "assets"


def appdata_dir() -> Path:
    """回傳 ``%APPDATA%\\MeetingScribe``。"""
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / APP_NAME


def localappdata_dir() -> Path:
    """回傳 ``%LOCALAPPDATA%\\MeetingScribe``（工作資料夾、日誌）。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME


def user_config_path() -> Path:
    """回傳使用者設定檔路徑。"""
    return appdata_dir() / "config.toml"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """把 override 遞迴合併到 base 的複本。"""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config(user_path: Path | None = None) -> dict[str, Any]:
    """讀取出廠值並合併使用者設定。"""
    with open(assets_dir() / "default_config.toml", "rb") as f:
        cfg = tomllib.load(f)
    path = user_path or user_config_path()
    if path.exists():
        with open(path, "rb") as f:
            cfg = _deep_merge(cfg, tomllib.load(f))
    return cfg


def init_user_config() -> Path:
    """若使用者設定檔不存在，複製一份出廠設定（保留註解）供編輯。"""
    path = user_config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(assets_dir() / "default_config.toml", path)
    return path


def get_secret(name: str) -> str | None:
    """讀取金鑰：環境變數優先，其次 Windows 認證管理員。"""
    env = os.environ.get(f"MEETINGSCRIBE_{name.upper()}")
    if env:
        return env
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, name)
    except Exception:  # noqa: BLE001 - keyring 後端不可用時視為未設定
        return None


def set_secret(name: str, value: str) -> None:
    """把金鑰存入 Windows 認證管理員。"""
    import keyring

    keyring.set_password(KEYRING_SERVICE, name, value)


def mask_secret(value: str | None) -> str:
    """只顯示末 4 碼。"""
    if not value:
        return "（未設定）"
    return "****" + value[-4:]
