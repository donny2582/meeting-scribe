"""建立桌面捷徑（開發版以 pythonw 啟動；打包後直接指向 exe）。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from meeting_scribe.config import app_root, localappdata_dir

SHORTCUT_NAME = "MeetingScribe 會議紀錄"

_PS_SCRIPT = (
    "$ws = New-Object -ComObject WScript.Shell; "
    "$s = $ws.CreateShortcut($env:MS_LNK); "
    "$s.TargetPath = $env:MS_TARGET; $s.Arguments = $env:MS_ARGS; "
    "$s.WorkingDirectory = $env:MS_CWD; $s.IconLocation = $env:MS_ICON; "
    "$s.Description = $env:MS_DESC; $s.Save()"
)


def _desktop() -> Path:
    import ctypes

    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0 and buf.value:  # CSIDL_DESKTOPDIRECTORY
        return Path(buf.value)
    return Path.home() / "Desktop"


def launch_target() -> tuple[Path, str, Path]:
    """回傳（執行檔, 參數, 工作資料夾）。"""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        return exe, "", exe.parent
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return (pythonw if pythonw.exists() else exe), "-m meeting_scribe", app_root()


def create_desktop_shortcut() -> Path:
    """在桌面建立（或覆蓋）捷徑，回傳捷徑路徑。"""
    if os.name != "nt":
        raise RuntimeError("只支援 Windows")
    from meeting_scribe.gui.icon import write_ico

    icon = write_ico(localappdata_dir() / "meeting_scribe.ico")
    target, args, cwd = launch_target()
    link = _desktop() / f"{SHORTCUT_NAME}.lnk"
    env = dict(os.environ, MS_LNK=str(link), MS_TARGET=str(target), MS_ARGS=args, MS_CWD=str(cwd),
               MS_ICON=f"{icon},0", MS_DESC="會議錄音轉逐字稿與會議紀錄")
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PS_SCRIPT],
        env=env, capture_output=True, creationflags=0x08000000, check=False,
    )
    if proc.returncode != 0 or not link.exists():
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace")[-300:] or "PowerShell 執行失敗")
    return link
