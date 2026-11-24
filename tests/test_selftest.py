"""第 5 階段：安裝後自我檢查（不需金鑰，在本機完成一次處理）與程式圖示。"""

import struct
from pathlib import Path

import pytest

from meeting_scribe.audio.preprocess import AudioError, find_ffmpeg


def _has_ffmpeg() -> bool:
    try:
        find_ffmpeg()
    except AudioError:
        return False
    return True


@pytest.mark.skipif(not _has_ffmpeg(), reason="需要 ffmpeg")
def test_selftest_passes(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from meeting_scribe.cli import main

    QApplication.instance() or QApplication([])
    assert main(["selftest", "--out", str(tmp_path / "out")]) == 0
    report = (tmp_path / "out" / "selftest_report.txt").read_text(encoding="utf-8")
    assert "結果：全部通過" in report and "[失敗]" not in report
    assert list((tmp_path / "out").glob("逐字稿_安裝自我檢查_*.docx"))
    assert (tmp_path / "out" / "心智圖_安裝自我檢查.png").is_file()


def test_icon_uses_custom_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtGui import QColor, QPixmap
    from PySide6.QtWidgets import QApplication

    from meeting_scribe.gui import icon

    QApplication.instance() or QApplication([])
    custom = tmp_path / "icon.png"
    red = QPixmap(64, 64)
    red.fill(QColor("red"))
    red.save(str(custom))
    monkeypatch.setattr(icon, "icon_image_path", lambda: custom)
    assert icon.make_pixmap(32).toImage().pixelColor(16, 16) == QColor("red")
    ico = icon.write_ico(tmp_path / "a.ico", sizes=(16, 256))
    _reserved, kind, count = struct.unpack("<HHH", ico.read_bytes()[:6])
    assert (kind, count) == (1, 2)
    monkeypatch.setattr(icon, "icon_image_path", lambda: tmp_path / "none.png")
    assert icon.make_pixmap(32).toImage().pixelColor(16, 16) != QColor("red")  # 沒有圖檔時用內建圖示
