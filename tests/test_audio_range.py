"""語音範圍：拉桿、試聽播放器、只轉錄範圍、時間碼對應原始錄音。"""

import os
import struct
import wave
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from meeting_scribe import checkpoint, tasks
from meeting_scribe.audio import preprocess
from meeting_scribe.config import load_config
from meeting_scribe.gui.audio_range import AudioRangePicker, RangeSlider, hms
from meeting_scribe.models import RawSegment, SttResult
from meeting_scribe.postprocess.lang_detect import LanguageRegistry
from meeting_scribe.transcript_io import from_stt_result


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_hms() -> None:
    assert hms(0) == "00:00:00" and hms(3725.4) == "01:02:05"


def test_slider_clamps_and_min_span(app: QApplication) -> None:
    s = RangeSlider()
    s.set_duration(100)
    assert (s.start, s.end) == (0, 100)
    s.set_range(-5, 500)
    assert (s.start, s.end) == (0, 100)
    s.set_range(50, 50.2)  # 至少 1 秒
    assert s.end - s.start == pytest.approx(1.0)


def test_slider_drag_handles_and_seek(app: QApplication) -> None:
    s = RangeSlider()
    s.resize(424, 44)  # 軌道寬 400（左右各留 12）
    s.set_duration(400)
    got: list[tuple[float, float]] = []
    seeks: list[float] = []
    s.range_changed.connect(lambda a, b: got.append((a, b)))
    s.seek_requested.connect(seeks.append)
    y = 22
    QTest.mousePress(s, Qt.MouseButton.LeftButton, pos=QPoint(12, y))  # 抓起點把手
    QTest.mouseMove(s, QPoint(112, y))
    QTest.mouseRelease(s, Qt.MouseButton.LeftButton, pos=QPoint(112, y))
    assert s.start == pytest.approx(100, abs=1.5)
    QTest.mousePress(s, Qt.MouseButton.LeftButton, pos=QPoint(412, y))  # 抓終點把手
    QTest.mouseMove(s, QPoint(312, y))
    QTest.mouseRelease(s, Qt.MouseButton.LeftButton, pos=QPoint(312, y))
    assert s.end == pytest.approx(300, abs=1.5) and got
    QTest.mouseClick(s, Qt.MouseButton.LeftButton, pos=QPoint(212, y))  # 中間點一下 → 跳到該處
    assert seeks and seeks[-1] == pytest.approx(200, abs=1.5)
    assert (s.start, s.end) == (pytest.approx(100, abs=1.5), pytest.approx(300, abs=1.5))


def test_picker_clip_and_time_edits(app: QApplication) -> None:
    p = AudioRangePicker()
    assert p.clip() is None and not p.play_button.isEnabled()
    p.set_duration(7200)
    assert p.clip() is None and "整段" in p.summary.text()
    p.slider.set_range(600, 4800)
    assert p.clip() == (600, 4800)
    assert p.start_edit.time().toString("HH:mm:ss") == "00:10:00"
    assert "已選 <b>01:10:00</b>" in p.summary.text()
    from PySide6.QtCore import QTime

    p.end_edit.setTime(QTime(1, 30, 0))
    p._on_edits()
    assert p.clip() == (600, 5400)
    p.slider.set_position(1200)
    p.slider.set_range(p.slider.position, p.slider.end)  # 以播放位置為起點
    assert p.clip()[0] == 1200


def _tone(path: Path, seconds: float = 3.0, rate: int = 8000) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", 0) for _ in range(int(seconds * rate))))
    return path


def test_picker_loads_real_file_duration(app: QApplication, tmp_path: Path) -> None:
    p = AudioRangePicker()
    p.load(_tone(tmp_path / "靜音.wav"))
    for _ in range(200):
        QCoreApplication.processEvents()
        if p.slider.duration > 0:
            break
        QTest.qWait(10)
    if p.slider.duration <= 0:
        pytest.skip("這個環境沒有可用的多媒體後端")
    assert p.slider.duration == pytest.approx(3.0, abs=0.2)
    assert p.play_button.isEnabled()
    p.load(None)
    assert p.slider.duration == 0 and not p.play_button.isEnabled()


# ---- 只轉錄範圍、時間碼對應原始錄音 ----
def test_preprocess_uses_clip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    src = tmp_path / "a.m4a"
    src.write_bytes(b"x")
    cmds: list[list[str]] = []

    class Proc:
        returncode = 0
        stderr = b"Duration: 00:00:10.00,"

    def run(cmd: list[str]) -> Proc:
        cmds.append(cmd)
        if cmd[-1].endswith(".flac"):
            Path(cmd[-1]).write_bytes(b"f")
        return Proc()

    monkeypatch.setattr(preprocess, "_run", run)
    preprocess.preprocess_audio(src, tmp_path / "o.flac", Path("ffmpeg"), clip=(600.0, 4800.0))
    cmd = cmds[1]
    assert cmd[cmd.index("-ss") + 1] == "600.00" and cmd[cmd.index("-t") + 1] == "4200.00"
    assert cmd.index("-ss") < cmd.index("-i")  # 先跳到起點再讀檔（快）
    with pytest.raises(preprocess.AudioError, match="太短"):
        preprocess.preprocess_audio(src, tmp_path / "o2.flac", Path("ffmpeg"), clip=(5.0, 5.5))


def test_transcribe_with_clip_offsets_timecodes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "會議.m4a"
    audio.write_bytes(b"x")
    clips: list[Any] = []

    class FakeEngine:
        display_name = "假引擎"

        def transcribe(self, flac: Path, *a: Any, **k: Any) -> SttResult:
            return SttResult(engine="soniox", engine_version="v", audio_duration=4200, elapsed_sec=1,
                             locales=["zh-TW"], segments=[RawSegment(start=0, end=5, speaker="講者1", text="開始"),
                                                          RawSegment(start=12.5, end=20, speaker="講者2", text="好")])

        def price_per_hour(self) -> float:
            return 0.1

    monkeypatch.setattr(tasks, "find_ffmpeg", lambda cfg: Path("ffmpeg"))
    monkeypatch.setattr(tasks, "preprocess_audio",
                        lambda src, dst, ff, clip=None: clips.append(clip) or dst.write_bytes(b"f"))
    monkeypatch.setattr(tasks, "create_engine", lambda name, cfg, ff: FakeEngine())
    out = tasks.run_transcribe_job("soniox", audio, ["zh"], [], load_config(), LanguageRegistry.load(),
                                   tmp_path / "out", clip=(600.0, 4800.0))
    r = out.result
    assert clips == [(600.0, 4800.0)]
    assert [(s.start, s.end) for s in r.segments] == [(600, 605), (612.5, 620)]  # 不是從 0 開始
    assert (r.clip_start, r.clip_end) == (600, 4800)
    txt = out.txt_path.read_text(encoding="utf-8-sig")
    assert "轉錄範圍：00:10:00–01:20:00" in txt and "[00:10:00] 講者1：開始" in txt
    doc = from_stt_result(r, out.folder)
    assert "範圍 00:10:00–01:20:00" in doc.source
    # 不同範圍使用不同的暫存，不會誤用整段的結果
    assert checkpoint.audio_dir(audio, (600.0, 4800.0)) != checkpoint.audio_dir(audio)


def test_transcribe_page_passes_clip(app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    from meeting_scribe.gui.main_window import MainWindow

    for kind in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, kind, lambda *a, **k: QMessageBox.StandardButton.Yes)
    win = MainWindow(secret_store={"get_secret": lambda n: None, "set_secret": lambda n, v: None,
                                   "delete_secret": lambda n: None})
    monkeypatch.setattr(win, "prepare_run", lambda engines: win.cfg)
    calls: list[Any] = []
    monkeypatch.setattr(tasks, "run_transcribe_job", lambda *a, **k: calls.append(k.get("clip")) or (_ for _ in ()).throw(RuntimeError("stop")))
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    page = win.transcribe
    page.audio.set_path(audio)
    page.range_picker.set_duration(3600)
    page.range_picker.slider.set_range(300, 1800)
    page.start()
    for _ in range(100):
        QCoreApplication.processEvents()
        if not page.panel.running:
            break
        QTest.qWait(10)
    assert calls == [(300, 1800)]
    assert "範圍 00:05:00–00:30:00" in page.panel.log.toPlainText()
    win.close()
