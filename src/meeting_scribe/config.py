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
    "soniox_api_key": "Soniox API 金鑰",
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


#: 每個 STT 引擎需要的金鑰
ENGINE_KEYS: dict[str, str] = {
    "azure": "azure_speech_key",
    "openai": "openai_api_key",
    "soniox": "soniox_api_key",
}


def documents_dir() -> Path:
    """回傳使用者的「文件」資料夾（支援 OneDrive 重新導向）。"""
    if sys.platform == "win32":
        import ctypes

        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:  # CSIDL_PERSONAL
            return Path(buf.value)
    return Path.home() / "Documents"


def output_dir(cfg: dict[str, Any]) -> Path:
    """輸出資料夾：設定值，空白時為「文件\\MeetingScribe\\輸出」。"""
    configured = (cfg.get("general", {}).get("output_dir") or "").strip()
    return Path(configured) if configured else documents_dir() / APP_NAME / "輸出"


def default_glossary_names() -> set[str]:
    """程式內建的預設詞庫名稱（例：道安業務），不可刪除。"""
    return {p.stem.removeprefix("seed_") for p in (assets_dir() / "glossary").glob("seed_*.csv")}


def jobs_dir() -> Path:
    """工作資料夾（SPEC 11）：``%LOCALAPPDATA%\\MeetingScribe\\jobs``。放程式用的檔案，不放在「文件」裡。"""
    return localappdata_dir() / "jobs"


def user_glossary_dir() -> Path:
    """使用者詞庫資料夾 ``%APPDATA%\\MeetingScribe\\glossary``。

    預設詞庫不存在時（第一次使用，或在檔案總管被刪除）自動從初始詞庫複製；已存在則不覆蓋，保留使用者累積的內容。
    """
    path = appdata_dir() / "glossary"
    path.mkdir(parents=True, exist_ok=True)
    for seed in (assets_dir() / "glossary").glob("seed_*.csv"):
        target = path / seed.name.removeprefix("seed_")
        if not target.exists():
            shutil.copyfile(seed, target)
    return path


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


def save_user_overrides(updates: dict[str, Any], user_path: Path | None = None) -> Path:
    """把 updates 合併寫入使用者設定檔（只寫入要覆寫的項目）。"""
    import tomli_w

    path = user_path or user_config_path()
    current: dict[str, Any] = {}
    if path.exists():
        with open(path, "rb") as f:
            current = tomllib.load(f)
    merged = _deep_merge(current, updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        tomli_w.dump(merged, f)
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


def delete_secret(name: str) -> None:
    """從 Windows 認證管理員刪除金鑰（不存在時忽略）。"""
    import keyring
    from keyring.errors import PasswordDeleteError

    try:
        keyring.delete_password(KEYRING_SERVICE, name)
    except PasswordDeleteError:
        pass


def mask_secret(value: str | None) -> str:
    """只顯示末 4 碼。"""
    if not value:
        return "（未設定）"
    return "****" + value[-4:]
