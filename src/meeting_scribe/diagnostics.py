"""錯誤安全網與問題回報（不依賴圖形介面，CLI 也可使用）。

- 日誌：``%LOCALAPPDATA%\\MeetingScribe\\logs\\meeting_scribe.log``，每個檔案最多 1 MB、保留 5 個舊檔；
  每次啟動先寫一行版本與系統資訊；未預期的錯誤記下完整的錯誤位置（traceback）。不記錄逐字稿內容。
- 已知錯誤（金鑰、餘額、網路、檔案格式…）已有清楚的中文說明；其他都視為「未預期的錯誤」（程式的 bug），
  要記錄完整細節並提示使用者回報。
- 設定檔檢查：使用者手動編輯 config.toml 寫錯格式時，說明是第幾行、常見原因，並可備份後改用出廠設定啟動。
- 問題回報檔：把日誌、版本與系統資訊、設定（不含金鑰與最近使用的人名地點）打包成 zip，由使用者自行寄送；
  不含錄音、逐字稿與金鑰，也不會自動上傳到任何地方。
"""

from __future__ import annotations

import logging
import logging.handlers
import platform
import sys
import threading
import traceback
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from meeting_scribe import __version__
from meeting_scribe.config import APP_NAME, documents_dir, localappdata_dir, user_config_path

log = logging.getLogger("meeting_scribe.diagnostics")

LOG_FILE = "meeting_scribe.log"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 5

#: 未預期錯誤時給使用者看的說明
UNEXPECTED_HINT = "這是程式的問題，不是您的操作錯誤。詳細資料已記錄在日誌，可以按「回報問題」產生回報檔寄給開發者。"


def log_dir() -> Path:
    return localappdata_dir() / "logs"


def app_version() -> str:
    return __version__


def edition() -> str:
    return "安裝版" if getattr(sys, "frozen", False) else "開發版"


def setup_logging(verbose: bool = False, console: bool = True) -> Path:
    """設定日誌（自動輪替），並寫入一行啟動資訊。回傳日誌檔路徑。"""
    folder = log_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / LOG_FILE
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
                                              encoding="utf-8", delay=True)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handlers: list[logging.Handler] = [fh]
    if console and sys.stderr is not None:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("%(message)s"))
        handlers.append(ch)
    root = logging.getLogger()
    for old in root.handlers:
        old.close()
    root.handlers[:] = handlers
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log.info("===== %s %s 啟動（%s）｜%s｜Python %s =====", APP_NAME, app_version(), edition(),
             platform.platform(), platform.python_version())
    return path


# ---- 除錯模式 ----
def debug_enabled(cfg: dict[str, Any] | None = None) -> bool:
    """除錯模式：設定 [general] debug = true，或環境變數 MEETINGSCRIBE_DEBUG=1。"""
    import os

    if os.environ.get("MEETINGSCRIBE_DEBUG", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    return bool((cfg or {}).get("general", {}).get("debug", False))


def apply_debug(on: bool) -> None:
    """立即切換日誌詳細程度（不必重新開啟程式）。"""
    logging.getLogger().setLevel(logging.DEBUG if on else logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 第三方套件的細節不需要
    log.info("除錯模式：%s", "開啟（日誌會記錄詳細的處理過程，不含逐字稿內容）" if on else "關閉")


# ---- 錯誤分類與記錄 ----
def expected_errors() -> tuple[type[BaseException], ...]:
    """已經有清楚中文說明的錯誤類型（不是程式的 bug）。"""
    from meeting_scribe.audio.preprocess import AudioError
    from meeting_scribe.llm.base import LLMError
    from meeting_scribe.stt.base import SttError
    from meeting_scribe.summarize.summarizer import SummaryError
    from meeting_scribe.transcript_io import ImportError_
    from meeting_scribe.usage import BudgetExceeded

    return (SttError, LLMError, AudioError, SummaryError, ImportError_, BudgetExceeded, OSError)


def is_expected(exc: BaseException) -> bool:
    return isinstance(exc, expected_errors())


def format_error(exc: BaseException) -> str:
    """完整的錯誤細節（給日誌與回報檔）。"""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()


def describe(exc: BaseException) -> str:
    """給使用者看的一句話：已知錯誤用原本的說明；未預期的錯誤說明是程式問題並提示回報。"""
    if is_expected(exc):
        return str(exc) or type(exc).__name__
    detail = str(exc)[:200]
    return f"程式發生未預期的錯誤（{type(exc).__name__}{'：' + detail if detail else ''}）。{UNEXPECTED_HINT}"


def log_exception(exc: BaseException, where: str = "") -> None:
    """記錄錯誤：已知錯誤記一行說明；未預期的錯誤記下完整的錯誤位置。"""
    prefix = f"{where}：" if where else ""
    if is_expected(exc):
        log.warning("%s%s", prefix, str(exc) or type(exc).__name__)
    else:
        log.error("%s未預期的錯誤 %s %s\n%s", prefix, APP_NAME, app_version(), format_error(exc))


ErrorCallback = Callable[[BaseException, str], None]


def install_excepthooks(on_error: ErrorCallback | None = None) -> None:
    """攔截所有沒有被處理的錯誤（主執行緒、背景執行緒）：記錄到日誌，再交給 on_error（例如顯示說明視窗）。"""

    def handle(exc: BaseException, where: str) -> None:
        if isinstance(exc, KeyboardInterrupt | SystemExit):
            return
        log_exception(exc, where)
        if on_error is not None:
            try:
                on_error(exc, where)
            except Exception:
                log.exception("顯示錯誤說明失敗")

    def sys_hook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        if exc.__traceback__ is None:
            exc = exc.with_traceback(tb)
        handle(exc, "")

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None:
            name = args.thread.name if args.thread else "背景執行緒"
            handle(args.exc_value, f"背景工作（{name}）")

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook


# ---- 設定檔檢查 ----
def check_user_config(path: Path | None = None) -> str | None:
    """檢查使用者設定檔的格式；有錯時回傳中文說明，沒問題回傳 None。"""
    import tomllib

    path = path or user_config_path()
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        line = getattr(exc, "lineno", None)
        where = f"第 {line} 行" if line else "某一行"
        hint = ""
        if "\\" in str(exc) or "escape" in str(exc).lower():
            hint = ("\n\n常見原因：資料夾路徑的反斜線「\\」要寫成兩個「\\\\」，或改用「/」，"
                    "例如 \"D:/會議\" 或 \"D:\\\\會議\"。")
        return f"設定檔 {path.name} 的{where}格式不正確（{exc}）。{hint}"
    except (OSError, UnicodeDecodeError) as exc:
        return f"無法讀取設定檔 {path.name}：{exc}"
    return None


def backup_user_config(path: Path | None = None) -> Path:
    """把有問題的設定檔改名備份（之後用出廠設定啟動），回傳備份檔路徑。"""
    path = path or user_config_path()
    backup = path.with_name(f"{path.stem}_格式錯誤_{datetime.now():%Y%m%d_%H%M%S}.toml")
    path.replace(backup)
    log.warning("設定檔格式錯誤，已備份為 %s，改用出廠設定", backup.name)
    return backup


# ---- 問題回報 ----
def system_info() -> list[str]:
    """版本與環境（不含金鑰、錄音與逐字稿）。"""
    from meeting_scribe.config import SECRET_NAMES, get_secret, load_config

    lines = [
        f"程式：{APP_NAME} {app_version()}（{edition()}）",
        f"執行檔：{sys.executable}",
        f"Windows：{platform.platform()}",
        f"Python：{platform.python_version()}",
        f"除錯模式：{'開啟' if logging.getLogger().isEnabledFor(logging.DEBUG) else '關閉'}",
        f"產生時間：{datetime.now():%Y-%m-%d %H:%M:%S}",
    ]
    try:
        cfg = load_config()
        lines.append(f"語音轉錄引擎：{cfg.get('stt', {}).get('default_engine', '')}")
        lines.append(f"文字整理服務：{cfg.get('llm', {}).get('provider', '')}")
    except Exception as exc:  # noqa: BLE001 - 設定檔有問題也要能產生回報
        lines.append(f"設定檔：無法讀取（{exc}）")
    try:
        for name, label in SECRET_NAMES.items():
            lines.append(f"{label}：{'已設定' if get_secret(name) else '未設定'}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"金鑰：無法檢查（{exc}）")
    try:
        from meeting_scribe.audio.preprocess import find_ffmpeg

        lines.append(f"ffmpeg：{find_ffmpeg()}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"ffmpeg：找不到（{str(exc).splitlines()[0]}）")
    return lines


def _sanitized_config(path: Path) -> str:
    """使用者設定檔的內容，移除 [ui] 區塊（最近使用的地點、主席、紀錄人員姓名）。金鑰本來就不在設定檔裡。"""
    import tomllib

    import tomli_w

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception:  # noqa: BLE001 - 格式錯誤時附上原文，方便找出是哪一行
        return path.read_text(encoding="utf-8", errors="replace")
    data.pop("ui", None)
    return tomli_w.dumps(data)


def reports_dir() -> Path:
    return documents_dir() / APP_NAME / "問題回報"


def make_report(dest: Path | None = None, note: str = "") -> Path:
    """產生問題回報檔（zip），回傳路徑。內容：版本與系統資訊、日誌、設定（不含金鑰與人名地點）。"""
    folder = dest or reports_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{APP_NAME}_問題回報_{datetime.now():%Y%m%d_%H%M%S}.zip"
    info = system_info()
    if note:
        info += ["", "錯誤摘要：", note]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("版本與系統資訊.txt", "\n".join(info) + "\n")
        for f in sorted(log_dir().glob(f"{LOG_FILE}*")):
            z.write(f, f"logs/{f.name}")
        cfg = user_config_path()
        if cfg.exists():
            z.writestr("config（已移除人名地點）.toml", _sanitized_config(cfg))
        z.writestr("說明.txt", "這個檔案只包含程式版本、系統資訊、日誌與設定，"
                              "不含錄音、逐字稿與 API 金鑰。請寄給開發者協助排除問題。\n")
    log.info("已產生問題回報檔：%s", path.name)
    return path
