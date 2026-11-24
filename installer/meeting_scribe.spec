# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 設定（onedir，SPEC 4 技術選型）。由 scripts\build.ps1 呼叫，請勿直接修改產出的 build\、dist\。
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).parent  # noqa: F821 - PyInstaller 提供 SPECPATH
SRC = ROOT / "src"
ICON = ROOT / "build" / "meeting_scribe.ico"

datas = [
    (str(ROOT / "assets"), "assets"),  # 設定、語言、模型清單、詞庫、ffmpeg\ffmpeg.exe
    (str(SRC / "meeting_scribe" / "summarize" / "prompts"), "meeting_scribe/summarize/prompts"),
]
datas += collect_data_files("opencc")  # 繁簡轉換字典
datas += collect_data_files("docx")  # python-docx 的預設範本

a = Analysis(  # noqa: F821
    [str(ROOT / "installer" / "launch.py")],
    pathex=[str(SRC)],
    datas=datas,
    hiddenimports=["keyring.backends.Windows", "PySide6.QtMultimedia"],
    excludes=["tkinter", "pytest", "_pytest", "numpy", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
              "PySide6.QtPdf", "PySide6.Qt3DCore", "PySide6.QtCharts", "PySide6.QtDataVisualization"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MeetingScribe",
    console=False,
    icon=str(ICON) if ICON.exists() else None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="MeetingScribe", upx=False)  # noqa: F821
