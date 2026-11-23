"""程式進入點：有參數時執行 CLI，沒有參數時啟動 GUI（第 4 階段）。"""

from __future__ import annotations

import sys


def main() -> int:
    """依命令列參數決定啟動 CLI 或 GUI。"""
    if len(sys.argv) > 1:
        from meeting_scribe.cli import main as cli_main

        return cli_main(sys.argv[1:])
    print("圖形介面將於第 4 階段實作，目前請使用命令列：python -m meeting_scribe --help")
    return 0


if __name__ == "__main__":
    sys.exit(main())
