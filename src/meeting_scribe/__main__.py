"""程式進入點：有參數時執行 CLI，沒有參數時開啟圖形介面。

圖形介面最外層也有錯誤保護：連圖形介面元件都無法載入時（例如安裝檔損毀），
記錄到日誌並用 Windows 內建的訊息視窗說明，不會無聲無息地結束。
"""

from __future__ import annotations

import sys


def _fatal(exc: BaseException) -> int:
    """最後一道防線：寫入日誌並顯示 Windows 訊息視窗。"""
    detail = f"{type(exc).__name__}：{exc}"
    try:
        from meeting_scribe import diagnostics

        diagnostics.log_exception(exc, "啟動")
        where = str(diagnostics.log_dir())
    except Exception:  # noqa: BLE001 - 連日誌都無法寫入時仍要顯示訊息
        where = "%LOCALAPPDATA%\\MeetingScribe\\logs"
    message = (f"MeetingScribe 無法啟動。\n\n{detail}\n\n詳細資料已記錄在日誌：\n{where}\n\n"
               "若重新安裝後仍無法啟動，請把日誌資料夾寄給開發者。")
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "MeetingScribe", 0x10)
    elif sys.stderr is not None:
        print(message, file=sys.stderr)
    return 1


def main() -> int:
    """依命令列參數決定啟動 CLI 或 GUI。"""
    if len(sys.argv) > 1:
        from meeting_scribe.cli import main as cli_main

        return cli_main(sys.argv[1:])
    try:
        from meeting_scribe.gui.main_window import run_gui

        return run_gui()
    except Exception as exc:  # noqa: BLE001 - 圖形介面本身無法啟動
        return _fatal(exc)


if __name__ == "__main__":
    sys.exit(main())