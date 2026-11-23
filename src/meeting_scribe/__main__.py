"""程式進入點：有參數時執行 CLI，沒有參數時開啟圖形介面。

完整主視窗於第 4 階段實作；目前沒有參數時先開啟設定視窗（輸入 API 金鑰等）。
"""

from __future__ import annotations

import sys


def main() -> int:
    """依命令列參數決定啟動 CLI 或 GUI。"""
    if len(sys.argv) > 1:
        from meeting_scribe.cli import main as cli_main

        return cli_main(sys.argv[1:])
    from meeting_scribe.gui.settings import run_settings

    return run_settings()


if __name__ == "__main__":
    sys.exit(main())
