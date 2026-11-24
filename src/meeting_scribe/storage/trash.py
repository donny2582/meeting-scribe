"""把檔案移到 Windows 資源回收筒（可以從回收筒還原），不直接永久刪除。"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400


def move_to_recycle_bin(path: Path) -> None:
    """把檔案移到資源回收筒；失敗時拋出 OSError。"""
    if sys.platform != "win32":
        raise OSError("只支援 Windows 資源回收筒")
    if not path.exists():
        raise FileNotFoundError(str(path))
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = str(path.resolve()) + "\0"  # 需要以兩個 NUL 結尾，ctypes 會再補一個
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0 or op.fAnyOperationsAborted or path.exists():
        raise OSError(f"無法移到資源回收筒（代碼 {result}），檔案可能正被 Excel 開啟")
