"""語音範圍拉桿＋試聽播放器（「轉錄」分頁使用）。

- 拉桿：兩端藍色把手設定要轉錄的範圍（範圍外以紅色斜線表示不轉錄）；直線與三角形為播放位置，點一下或拖曳可跳到該處。
- 播放器：跳到起點、播放／暫停、停止、跳到終點；播放到範圍終點會自動停止，方便確認範圍。
- 也可以直接輸入起點／終點時間，或用「以播放位置為起點／終點」精準設定。
- 逐字稿的時間碼會加上起點，對應原始錄音的時間（不是從 0 開始）。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QTime, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPalette,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStyle,
    QTimeEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

MIN_SPAN = 1.0  # 範圍至少 1 秒
FALLBACK_MS = 5000  # 播放器 5 秒內讀不出長度時改用 ffmpeg
HANDLE_W = 12


def hms(sec: float) -> str:
    """秒 → 時:分:秒。"""
    s = max(0, round(sec))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


class RangeSlider(QWidget):
    """有兩個把手（起點、終點）與播放位置的拉桿。"""

    range_changed = Signal(float, float)
    seek_requested = Signal(float)

    def __init__(self) -> None:
        super().__init__()
        self.duration = 0.0
        self.start = 0.0
        self.end = 0.0
        self.position = 0.0
        self._drag: str | None = None
        self.setMinimumHeight(44)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("拖曳兩端的藍色把手設定要轉錄的範圍；點一下拉桿可以跳到該處試聽")

    # ---- 座標換算 ----
    def _track(self) -> QRectF:
        return QRectF(HANDLE_W, self.height() / 2 - 7, max(1.0, self.width() - 2 * HANDLE_W), 14)

    def _x(self, sec: float) -> float:
        t = self._track()
        return t.left() + (sec / self.duration if self.duration else 0) * t.width()

    def _sec(self, x: float) -> float:
        t = self._track()
        ratio = min(1.0, max(0.0, (x - t.left()) / t.width()))
        return ratio * self.duration

    # ---- 設定 ----
    def set_duration(self, duration: float) -> None:
        self.duration = max(0.0, duration)
        self.start, self.end, self.position = 0.0, self.duration, 0.0
        self.setEnabled(self.duration > 0)
        self.update()

    def set_range(self, start: float, end: float, emit: bool = True) -> None:
        start = min(max(0.0, start), max(0.0, self.duration - MIN_SPAN))
        end = max(min(self.duration, end), start + MIN_SPAN) if self.duration else 0.0
        if (start, end) != (self.start, self.end):
            self.start, self.end = start, end
            self.update()
            if emit:
                self.range_changed.emit(start, end)

    def set_position(self, sec: float) -> None:
        self.position = min(max(0.0, sec), self.duration)
        self.update()

    # ---- 繪製 ----
    def paintEvent(self, _e: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = self._track()
        enabled = self.isEnabled() and self.duration > 0
        # 範圍外：紅色斜線（不轉錄）
        out = QColor("#8b2c2c" if enabled else "#888888")
        brush = QBrush(out, Qt.BrushStyle.BDiagPattern)
        clip = QPainterPath()
        clip.addRoundedRect(track, 7, 7)
        p.setClipPath(clip)
        p.fillRect(track, QColor(out.red(), out.green(), out.blue(), 70))
        p.fillRect(track, brush)
        if enabled:
            sel = QRectF(self._x(self.start), track.top(), self._x(self.end) - self._x(self.start), track.height())
            p.fillRect(sel, QColor("#2f6fb8"))
        p.setClipping(False)
        if not enabled:
            return
        # 起點與終點把手
        for sec in (self.start, self.end):
            x = self._x(sec)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#5b9cf0"))
            p.drawRoundedRect(QRectF(x - HANDLE_W / 2, track.top() - 5, HANDLE_W, track.height() + 10), 4, 4)
        # 播放位置
        x = self._x(self.position)
        head = self.palette().color(QPalette.ColorRole.WindowText)  # 淺色主題為深色、深色主題為白色
        p.setPen(QPen(head, 2))
        p.drawLine(QPointF(x, track.top() - 7), QPointF(x, track.bottom() + 3))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(head)
        y = track.bottom() + 4
        p.drawPolygon(QPolygonF([QPointF(x, y), QPointF(x - 5, y + 7), QPointF(x + 5, y + 7)]))

    # ---- 滑鼠 ----
    def mousePressEvent(self, e: QMouseEvent) -> None:
        if not self.duration:
            return
        x = e.position().x()
        ds, de = abs(x - self._x(self.start)), abs(x - self._x(self.end))
        if min(ds, de) <= HANDLE_W:
            self._drag = "start" if ds <= de else "end"
        else:
            self._drag = "seek"
            self._seek(x)

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        x = e.position().x()
        if self._drag == "start":
            self.set_range(min(self._sec(x), self.end - MIN_SPAN), self.end)
        elif self._drag == "end":
            self.set_range(self.start, max(self._sec(x), self.start + MIN_SPAN))
        elif self._drag == "seek":
            self._seek(x)

    def mouseReleaseEvent(self, _e: QMouseEvent) -> None:
        self._drag = None

    def _seek(self, x: float) -> None:
        sec = self._sec(x)
        self.set_position(sec)
        self.seek_requested.emit(sec)


def _time_edit() -> QTimeEdit:
    edit = QTimeEdit()
    edit.setDisplayFormat("HH:mm:ss")
    edit.setButtonSymbols(QTimeEdit.ButtonSymbols.NoButtons)
    edit.setFixedWidth(86)
    edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return edit


def _to_qtime(sec: float) -> QTime:
    s = max(0, round(sec))
    return QTime(min(23, s // 3600), s % 3600 // 60, s % 60)


def _from_qtime(t: QTime) -> float:
    return float(t.hour() * 3600 + t.minute() * 60 + t.second())


class AudioRangePicker(QWidget):
    """語音範圍設定＋試聽播放器。"""

    range_changed = Signal(float, float)

    def __init__(self) -> None:
        super().__init__()
        self.path: Path | None = None
        self.player = None
        self._audio_out = None
        self._stop_at_end = True
        self.slider = RangeSlider()
        self.slider.range_changed.connect(self._on_slider_range)
        self.slider.seek_requested.connect(self.seek)

        style = self.style()

        def tool(icon: QStyle.StandardPixmap, tip: str) -> QToolButton:
            b = QToolButton()
            b.setIcon(style.standardIcon(icon))
            b.setToolTip(tip)
            b.setAutoRaise(False)
            b.setFixedSize(34, 30)
            return b

        self.to_start = tool(QStyle.StandardPixmap.SP_MediaSkipBackward, "跳到範圍起點")
        self.play_button = tool(QStyle.StandardPixmap.SP_MediaPlay, "播放／暫停（播到範圍終點會自動停止）")
        self.stop_button = tool(QStyle.StandardPixmap.SP_MediaStop, "停止並回到範圍起點")
        self.to_end = tool(QStyle.StandardPixmap.SP_MediaSkipForward, "跳到範圍終點前 5 秒（確認結尾）")
        self.to_start.clicked.connect(lambda: self.seek(self.slider.start))
        self.play_button.clicked.connect(self.toggle_play)
        self.stop_button.clicked.connect(self.stop)
        self.to_end.clicked.connect(lambda: self.seek(max(self.slider.start, self.slider.end - 5)))
        self.time_label = QLabel("--:--:-- / --:--:--")
        self.time_label.setMinimumWidth(150)

        self.start_edit = _time_edit()
        self.end_edit = _time_edit()
        self.start_edit.editingFinished.connect(self._on_edits)
        self.end_edit.editingFinished.connect(self._on_edits)
        set_start = QPushButton("以播放位置為起點")
        set_start.clicked.connect(lambda: self.slider.set_range(self.slider.position, self.slider.end))
        set_end = QPushButton("以播放位置為終點")
        set_end.clicked.connect(lambda: self.slider.set_range(self.slider.start, self.slider.position))
        reset = QPushButton("全部")
        reset.setToolTip("重設為整段錄音")
        reset.clicked.connect(lambda: self.slider.set_range(0, self.slider.duration))
        self.summary = QLabel("")
        self.status = QLabel("")
        self.status.setStyleSheet("color: palette(placeholder-text);")
        self.status.setWordWrap(True)

        controls = QHBoxLayout()
        for w in (self.to_start, self.play_button, self.stop_button, self.to_end):
            controls.addWidget(w)
        controls.addWidget(self.time_label)
        controls.addStretch(1)
        controls.addWidget(self.summary)
        edits = QHBoxLayout()
        edits.addWidget(QLabel("起點："))
        edits.addWidget(self.start_edit)
        edits.addWidget(set_start)
        edits.addSpacing(16)
        edits.addWidget(QLabel("終點："))
        edits.addWidget(self.end_edit)
        edits.addWidget(set_end)
        edits.addStretch(1)
        edits.addWidget(reset)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(controls)
        lay.addWidget(self.slider)
        lay.addLayout(edits)
        lay.addWidget(self.status)
        self._set_ready(False)

    # ---- 載入 ----
    def _ensure_player(self) -> bool:
        if self.player is not None:
            return True
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        except ImportError:
            return False
        self.player = QMediaPlayer(self)
        self._audio_out = QAudioOutput(self)
        self.player.setAudioOutput(self._audio_out)
        self.player.durationChanged.connect(lambda ms: self.set_duration(ms / 1000) if ms > 0 else None)
        self.player.positionChanged.connect(self._on_position)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.errorOccurred.connect(lambda _e, msg: self._fallback(msg))
        return True

    def load(self, path: Path | None) -> None:
        """載入錄音檔；None 表示清除。"""
        self.stop()
        self.path = path
        self.set_duration(0)
        if path is None or not path.is_file():
            self.status.setText("")
            return
        self.status.setText("讀取錄音長度中…")
        self._playable = True
        if self._ensure_player():
            self.player.setSource(QUrl.fromLocalFile(str(path)))  # type: ignore[union-attr]
            # 播放器讀不出長度（例如 amr、wma 等少見格式）時，改用 ffmpeg 讀取
            QTimer.singleShot(FALLBACK_MS, lambda p=path: p == self.path and self.slider.duration <= 0
                              and self._fallback("播放器無法讀取這個格式"))
        else:
            self._fallback("這台電腦無法使用試聽功能")

    def _fallback(self, reason: str) -> None:
        """無法試聽時：用 ffmpeg 讀取長度，仍可設定範圍並轉錄（只是不能播放）。"""
        path = self.path
        if path is None or not path.is_file():
            return
        self._playable = False
        self.stop()
        if self.slider.duration <= 0:
            from meeting_scribe.audio.preprocess import AudioError, find_ffmpeg, probe_duration

            try:
                duration = probe_duration(path, find_ffmpeg())
            except AudioError as exc:
                self.status.setText(str(exc).splitlines()[0])
                return
            self.set_duration(duration)
        for w in (self.play_button, self.stop_button):
            w.setEnabled(False)
        self.status.setText(f"這個格式無法試聽（{reason}），但可以轉錄；拖曳藍色把手或輸入時間設定範圍。")

    def set_duration(self, duration: float) -> None:
        """設定錄音長度並把範圍重設為全部。"""
        self.slider.set_duration(duration)
        for edit in (self.start_edit, self.end_edit):
            edit.setMaximumTime(_to_qtime(duration))
        self._sync_edits()
        self._set_ready(duration > 0)
        if duration > 0:
            self.status.setText("拖曳藍色把手設定要轉錄的範圍（預設整段），可以按播放試聽確認。")
            self.range_changed.emit(self.slider.start, self.slider.end)

    def _set_ready(self, ready: bool) -> None:
        playable = getattr(self, "_playable", True)
        for w in (self.to_start, self.play_button, self.stop_button, self.to_end, self.start_edit, self.end_edit):
            w.setEnabled(ready and (playable or w in (self.start_edit, self.end_edit)))
        self._update_labels()

    # ---- 範圍 ----
    def range(self) -> tuple[float, float]:
        return self.slider.start, self.slider.end

    def is_full(self) -> bool:
        """範圍是否為整段錄音（容許 0.5 秒誤差）。"""
        return self.slider.start < 0.5 and self.slider.end > self.slider.duration - 0.5

    def clip(self) -> tuple[float, float] | None:
        """要轉錄的範圍；整段時回傳 None。"""
        return None if self.slider.duration <= 0 or self.is_full() else self.range()

    def _on_slider_range(self, start: float, end: float) -> None:
        self._sync_edits()
        if self.player is not None and not (start <= self.slider.position <= end):
            self.seek(start)
        self.range_changed.emit(start, end)

    def _on_edits(self) -> None:
        self.slider.set_range(_from_qtime(self.start_edit.time()), _from_qtime(self.end_edit.time()))
        self._sync_edits()

    def _sync_edits(self) -> None:
        for edit, sec in ((self.start_edit, self.slider.start), (self.end_edit, self.slider.end)):
            edit.blockSignals(True)
            edit.setTime(_to_qtime(sec))
            edit.blockSignals(False)
        self._update_labels()

    def _update_labels(self) -> None:
        s = self.slider
        if s.duration <= 0:
            self.summary.setText("")
            self.time_label.setText("--:--:-- / --:--:--")
            return
        self.time_label.setText(f"{hms(s.position)} / {hms(s.duration)}")
        span = s.end - s.start
        self.summary.setText(f"已選 <b>{hms(span)}</b>（全長 {hms(s.duration)}）" if not self.is_full()
                             else f"整段 <b>{hms(s.duration)}</b>")

    # ---- 播放 ----
    def seek(self, sec: float) -> None:
        self.slider.set_position(sec)
        if self.player is not None:
            self.player.setPosition(int(sec * 1000))
        self._update_labels()

    def toggle_play(self) -> None:
        if self.player is None or self.slider.duration <= 0:
            return
        from PySide6.QtMultimedia import QMediaPlayer

        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            return
        pos = self.slider.position
        if pos < self.slider.start or pos >= self.slider.end - 0.3:
            self.seek(self.slider.start)
        self.player.play()

    def stop(self) -> None:
        if self.player is not None:
            self.player.pause()
        self.seek(self.slider.start)

    def _on_position(self, ms: int) -> None:
        sec = ms / 1000
        self.slider.set_position(sec)
        self._update_labels()
        if self._stop_at_end and sec >= self.slider.end and self.player is not None:
            from PySide6.QtMultimedia import QMediaPlayer

            if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.player.pause()
                self.status.setText(f"已播到範圍終點 {hms(self.slider.end)}。")

    def _on_state(self, state: object) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        playing = state == QMediaPlayer.PlaybackState.PlayingState
        icon = QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay
        self.play_button.setIcon(self.style().standardIcon(icon))
