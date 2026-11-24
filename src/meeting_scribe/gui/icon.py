"""程式圖示（藍底白色「紀」），視窗與桌面捷徑共用；用程式繪製，不需附圖檔。"""

from __future__ import annotations

import struct
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap


def make_pixmap(size: int = 64) -> QPixmap:
    """畫出 size × size 的圖示（以 64 × 64 座標設計後縮放）。"""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    p.scale(size / 64, size / 64)
    p.setBrush(QColor("#1f6feb"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 12, 12)
    p.setPen(QColor("white"))
    font = QFont("Microsoft JhengHei UI")
    font.setPixelSize(36)
    font.setWeight(QFont.Weight.Bold)
    p.setFont(font)
    p.drawText(0, 0, 64, 64, Qt.AlignmentFlag.AlignCenter, "紀")
    p.end()
    return pix


def make_icon() -> QIcon:
    """視窗圖示。"""
    icon = QIcon()
    for size in (16, 32, 48, 64, 256):
        icon.addPixmap(make_pixmap(size))
    return icon


def write_ico(path: Path, size: int = 256) -> Path:
    """存成 .ico（內嵌 PNG，Windows Vista 以後支援），供桌面捷徑使用。需先建立 QApplication。"""
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    make_pixmap(size).save(buf, "PNG")
    buf.close()
    png = bytes(data)
    dim = 0 if size >= 256 else size
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), 6 + 16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + entry + png)
    return path
