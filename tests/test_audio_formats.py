"""音檔格式：不以副檔名限制，交給 ffmpeg 判斷；沒有聲音、損毀的檔案擋下；無法試聽時仍可設定範圍。"""

import os
import subprocess
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from meeting_scribe.audio.preprocess import (
    SUPPORTED_EXTENSIONS,
    AudioError,
    find_ffmpeg,
    preprocess_audio,
    probe_duration,
)


def _ffmpeg() -> Path | None:
    try:
        return find_ffmpeg()
    except AudioError:
        return None


FF = _ffmpeg()
needs_ffmpeg = pytest.mark.skipif(FF is None, reason="需要 ffmpeg")


def _make(path: Path, *args: str, video: bool = False) -> Path:
    assert FF is not None
    inputs = ["-f", "lavfi", "-i", "color=c=blue:s=160x120:d=4"] if video else []
    inputs += ["-f", "lavfi", "-i", "sine=frequency=440:duration=4"]
    subprocess.run([str(FF), "-hide_banner", "-y", "-loglevel", "error", *inputs, *args, "-shortest", str(path)],
                   check=True, capture_output=True)
    return path


def test_common_formats_are_listed() -> None:
    for ext in (".aac", ".wma", ".ogg", ".opus", ".amr", ".aiff", ".mp3", ".m4a", ".wav", ".mov", ".mkv"):
        assert ext in SUPPORTED_EXTENSIONS


@needs_ffmpeg
@pytest.mark.parametrize("name,args", [
    ("錄音.aac", ["-c:a", "aac", "-f", "adts"]),
    ("錄音.wma", ["-c:a", "wmav2"]),
    ("錄音.ogg", ["-c:a", "libvorbis"]),
    ("錄音.amr", ["-ar", "8000", "-c:a", "libopencore_amrnb", "-b:a", "12.2k"]),
    ("錄音.未知副檔名", ["-f", "wav"]),  # 不在清單裡的副檔名也交給 ffmpeg
])
def test_formats_transcodable(tmp_path: Path, name: str, args: list[str]) -> None:
    src = _make(tmp_path / name, *args)
    info = preprocess_audio(src, tmp_path / "out.flac", FF, clip=(1.0, 3.0))  # type: ignore[arg-type]
    assert 1.5 < info.duration < 2.5


@needs_ffmpeg
def test_video_uses_audio_track(tmp_path: Path) -> None:
    src = _make(tmp_path / "會議錄影.mkv", video=True)
    assert 3.5 < preprocess_audio(src, tmp_path / "out.flac", FF).duration < 4.5  # type: ignore[arg-type]


@needs_ffmpeg
def test_no_audio_and_broken_files_are_rejected(tmp_path: Path) -> None:
    assert FF is not None
    silent = tmp_path / "只有影像.mp4"
    subprocess.run([str(FF), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x120:d=2",
                    "-c:v", "mpeg4", str(silent)], check=True, capture_output=True)
    with pytest.raises(AudioError, match="沒有聲音"):
        probe_duration(silent, FF)
    broken = tmp_path / "壞掉的.mp3"
    broken.write_bytes(b"not audio" * 100)
    with pytest.raises(AudioError, match="無法讀取"):
        probe_duration(broken, FF)


@needs_ffmpeg
def test_range_picker_falls_back_to_ffmpeg_when_player_unavailable(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QApplication

    from meeting_scribe.gui.audio_range import AudioRangePicker

    app = QApplication.instance() or QApplication([])
    src = _make(tmp_path / "錄音.amr", "-ar", "8000", "-c:a", "libopencore_amrnb", "-b:a", "12.2k")
    w = AudioRangePicker()
    monkeypatch.setattr(w, "_ensure_player", lambda: False)  # 模擬播放器無法使用這個格式
    w.load(src)
    assert 3.5 < w.slider.duration < 4.5  # 改用 ffmpeg 讀出長度
    assert not w.play_button.isEnabled() and w.start_edit.isEnabled()  # 不能試聽，但可以設定範圍
    assert "無法試聽" in w.status.text() and "可以轉錄" in w.status.text()
    w.slider.set_range(1.0, 3.0)
    assert w.clip() == (1.0, 3.0)
    del app
