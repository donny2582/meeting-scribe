"""PyInstaller 進入點：與 ``python -m meeting_scribe`` 相同（沒有參數開圖形介面，有參數執行 CLI）。"""

import sys

from meeting_scribe.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
