"""程式圖示，視窗、桌面捷徑與安裝檔共用。

有 ``assets\\icon.png`` 時使用這張圖（建議正方形、至少 256 × 256）；沒有時畫出藍底白色「紀」。
"""

from __future__ import annotations

import struct
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap

from meeting_scribe.config import assets_dir


def icon_image_path() -> Path:
    """自訂圖示的位置。"""
    return assets_dir() / "icon.png"


def make_pixmap(size: int = 64) -> QPixmap:
    """回傳 size × size 的圖示：有自訂圖檔就縮放置中，否則以 64 × 64 座標畫出後縮放。"""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    custom = QPixmap(str(icon_image_path())) if icon_image_path().is_file() else QPixmap()
    if not custom.isNull():
        scaled = custom.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        p = QPainter(pix)
        p.drawPixmap((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
        p.end()
        return pix
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


def write_ico(path: Path, sizes: tuple[int, ...] = (16, 24, 32, 48, 64, 256)) -> Path:
    """存成多尺寸 .ico（內嵌 PNG，Windows Vista 以後支援），供桌面捷徑與安裝檔使用。需先建立 QApplication。"""
    pngs: list[bytes] = []
    for size in sizes:
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        make_pixmap(size).save(buf, "PNG")
        buf.close()
        pngs.append(bytes(data))
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for size, png in zip(sizes, pngs, strict=True):
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + entries + b"".join(pngs))
    return path
