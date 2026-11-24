"""共用介面元件：說明小字、檔案選擇、語言勾選、執行面板、背景工作。"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from meeting_scribe.postprocess.lang_detect import LanguageRegistry

AUDIO_FILTER = "錄音檔 (*.mp3 *.m4a *.wav *.mp4 *.flac);;所有檔案 (*.*)"
CSV_FILTER = "CSV 檔 (*.csv);;所有檔案 (*.*)"

#: 主要按鈕（藍底白字）與共用樣式
APP_STYLE = """
QPushButton#primary { background: #1f6feb; color: white; border: none; border-radius: 4px;
                      padding: 7px 20px; font-weight: bold; }
QPushButton#primary:hover { background: #1a5fd0; }
QPushButton#primary:disabled { background: #9db7e0; }
QGroupBox { font-weight: bold; margin-top: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
QGroupBox QLabel, QGroupBox QCheckBox, QGroupBox QLineEdit, QGroupBox QComboBox { font-weight: normal; }
QToolButton { font-weight: bold; min-width: 26px; min-height: 22px; }
"""

OK_COLOR = "#2e7d32"
ERROR_COLOR = "#c62828"
WARN_COLOR = "#e65100"


def hint(text: str) -> QLabel:
    """灰色的說明小字。"""
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    lbl.setStyleSheet("color: palette(placeholder-text); font-weight: normal;")
    return lbl


def primary_button(text: str) -> QPushButton:
    """主要動作按鈕（藍色）。"""
    btn = QPushButton(text)
    btn.setObjectName("primary")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def open_path(path: Path) -> None:
    """用 Windows 預設程式開啟檔案或資料夾（CSV 會用 Excel 開啟）。"""
    if os.name == "nt":
        os.startfile(str(path))
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


class FilePicker(QWidget):
    """路徑欄位＋「瀏覽…」按鈕；可直接把檔案拖進欄位。

    mode：``open``（選檔案）、``save``（另存新檔）、``dir``（選資料夾）。
    """

    changed = Signal(str)

    def __init__(self, mode: str = "open", file_filter: str = "", placeholder: str = "",
                 title: str = "選擇檔案") -> None:
        super().__init__()
        self.mode, self.file_filter, self.title = mode, file_filter, title
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.textChanged.connect(self.changed.emit)
        self.edit.setAcceptDrops(False)
        self.setAcceptDrops(True)
        browse = QPushButton("瀏覽…")
        browse.clicked.connect(self.browse)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.edit, 1)
        lay.addWidget(browse)

    def path(self) -> Path | None:
        """目前的路徑；空白時回傳 None。"""
        text = self.edit.text().strip().strip('"')
        return Path(text) if text else None

    def set_path(self, path: Path | str | None) -> None:
        self.edit.setText(str(path) if path else "")

    def browse(self) -> None:
        """開啟選擇檔案或資料夾的視窗。"""
        current = self.path()
        start = str(current if current and current.exists() else (current.parent if current else Path.home()))
        if self.mode == "dir":
            chosen = QFileDialog.getExistingDirectory(self, self.title, start)
        elif self.mode == "save":
            chosen, _ = QFileDialog.getSaveFileName(self, self.title, start, self.file_filter)
        else:
            chosen, _ = QFileDialog.getOpenFileName(self, self.title, start, self.file_filter)
        if chosen:
            self.set_path(Path(chosen))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        urls = event.mimeData().urls()
        if urls:
            self.set_path(Path(urls[0].toLocalFile()))


class LanguagePicker(QWidget):
    """會議語言勾選：中文固定勾選，其他語言依 languages.toml。"""

    changed = Signal()

    def __init__(self, registry: LanguageRegistry, selected: list[str]) -> None:
        super().__init__()
        self.boxes: dict[str, QCheckBox] = {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        for rule in registry.languages.values():
            if not rule.selectable:
                continue
            cb = QCheckBox(rule.name + ("（固定）" if rule.base else ""))
            cb.setChecked(rule.base or rule.code in selected)
            cb.setEnabled(not rule.base)
            cb.toggled.connect(lambda _on: self.changed.emit())
            self.boxes[rule.code] = cb
            lay.addWidget(cb)
        lay.addStretch(1)
        self.setToolTip("只勾實際會出現的語言：候選語言越多，越容易把中文誤判成其他語言。")

    def codes(self) -> list[str]:
        return [c for c, cb in self.boxes.items() if cb.isChecked()]

    def set_codes(self, codes: list[str]) -> None:
        for c, cb in self.boxes.items():
            if cb.isEnabled():
                cb.setChecked(c in codes)


class ModelPicker(QWidget):
    """模型下拉選單＋右側「＋」按鈕（新增清單裡沒有的模型名稱）。

    options：[(模型 ID, 顯示名稱, 說明)]；recommended 的選項會標示「★ 建議」。
    """

    added = Signal(str)
    changed = Signal(str)

    def __init__(self, options: list[tuple[str, str, str]], recommended: str | None = None,
                 title: str = "新增模型") -> None:
        super().__init__()
        self.recommended = recommended
        self.title = title
        self.combo = QComboBox()
        self.combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.combo.currentIndexChanged.connect(lambda _i: self.changed.emit(self.value()))
        for model_id, label, note in options:
            self._add_item(model_id, label, note)
        self.plus = QToolButton()
        self.plus.setText("＋")
        self.plus.setToolTip("新增清單裡沒有的模型名稱（例如廠商推出的新版本）")
        self.plus.setCursor(Qt.CursorShape.PointingHandCursor)
        self.plus.clicked.connect(self.ask_new)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.combo, 1)
        lay.addWidget(self.plus)

    def _add_item(self, model_id: str, label: str, note: str = "") -> None:
        text = label if label == model_id else f"{label}（{model_id}）"
        if note:
            text += f"－{note}"
        if model_id == self.recommended:
            text += "　★ 建議"
        self.combo.addItem(text, model_id)

    def value(self) -> str:
        return str(self.combo.currentData() or "")

    def has(self, model_id: str) -> bool:
        return self.combo.findData(model_id) >= 0

    def add_option(self, model_id: str, note: str = "自行新增") -> None:
        """加入一個選項（已存在則略過）。"""
        if model_id and not self.has(model_id):
            self._add_item(model_id, model_id, note)

    def set_value(self, model_id: str) -> None:
        """選取指定模型；清單中沒有時先加入。"""
        if not model_id:
            return
        self.add_option(model_id, "設定檔中的模型")
        self.combo.setCurrentIndex(self.combo.findData(model_id))

    def ask_new(self) -> None:
        """跳出輸入框新增模型名稱，加入後立即選取。"""
        from PySide6.QtWidgets import QInputDialog

        text, ok = QInputDialog.getText(self, self.title, "模型名稱（請照廠商文件上的 ID 輸入，例：claude-sonnet-5-5）：")
        model_id = text.strip()
        if not ok or not model_id:
            return
        if any(ch.isspace() for ch in model_id):
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.warning(self, self.title, "模型名稱不能包含空白。")
            return
        is_new = not self.has(model_id)
        self.add_option(model_id)
        self.combo.setCurrentIndex(self.combo.findData(model_id))
        if is_new:
            self.added.emit(model_id)


class TaskSignals(QObject):
    """背景工作回報給介面的訊號（跨執行緒時 Qt 會自動排入主執行緒）。"""

    progress = Signal(float, str)
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()


class RunPanel(QWidget):
    """執行面板：進度條、狀態文字、經過時間、取消按鈕、執行紀錄。

    一次只執行一個工作；長時間工作在背景執行緒進行，介面不會卡住。
    """

    busy_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.signals = TaskSignals()
        self.signals.progress.connect(self._on_progress)
        self._cancel = threading.Event()
        self._running = False
        self._started = 0.0
        self._on_success: Callable[[Any], None] | None = None

        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setMaximumHeight(10)
        self.status = QLabel("尚未開始")
        self.elapsed = QLabel("")
        self.elapsed.setStyleSheet("color: gray;")
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(90)
        self.log.setPlaceholderText("執行紀錄會顯示在這裡（不含逐字稿內容）")

        top = QHBoxLayout()
        top.addWidget(self.status, 1)
        top.addWidget(self.elapsed)
        top.addWidget(self.cancel_button)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(top)
        lay.addWidget(self.bar)
        lay.addWidget(self.log, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self.signals.succeeded.connect(self._done_ok)
        self.signals.failed.connect(self._done_error)
        self.signals.cancelled.connect(self._done_cancelled)

    @property
    def running(self) -> bool:
        return self._running

    def append(self, text: str) -> None:
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {text}")

    def start(self, title: str, work: Callable[[Callable[[float, str], None], Callable[[], bool]], Any],
              on_success: Callable[[Any], None]) -> None:
        """在背景執行 work(progress, cancel)，完成後在主執行緒呼叫 on_success(結果)。"""
        from meeting_scribe.tasks import Cancelled

        self._running = True
        self._cancel.clear()
        self._on_success = on_success
        self._started = time.monotonic()
        self.bar.setRange(0, 0)  # 不確定進度時顯示忙碌動畫
        self.status.setText(f"{title}…")
        self.status.setStyleSheet("")
        self.cancel_button.setEnabled(True)
        self.append(f"開始：{title}")
        self._timer.start()
        self.busy_changed.emit(True)

        def run() -> None:
            try:
                result = work(self.signals.progress.emit, self._cancel.is_set)
            except Cancelled:
                self.signals.cancelled.emit()
            except Exception as exc:  # noqa: BLE001 - 所有錯誤都要顯示給使用者
                self.signals.failed.emit(str(exc) or type(exc).__name__)
            else:
                self.signals.succeeded.emit(result)

        threading.Thread(target=run, daemon=True).start()

    def cancel(self) -> None:
        """要求取消；會在目前步驟完成後停止。"""
        self._cancel.set()
        self.cancel_button.setEnabled(False)
        self.status.setText("取消中（等目前的步驟完成）…")

    def _on_progress(self, pct: float, msg: str) -> None:
        if pct > 0:
            self.bar.setRange(0, 100)
            self.bar.setValue(int(pct))
        if self._cancel.is_set():
            return
        if msg and msg != self.status.text():
            self.status.setText(msg)
            self.append(msg)

    def _tick(self) -> None:
        sec = int(time.monotonic() - self._started)
        self.elapsed.setText(f"已經過 {sec // 60}:{sec % 60:02d}")

    def _finish(self, text: str, color: str) -> None:
        self._running = False
        self._timer.stop()
        self._tick()
        self.cancel_button.setEnabled(False)
        self.bar.setRange(0, 100)
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color}; font-weight: bold;")
        self.append(text)
        self.busy_changed.emit(False)

    def _done_ok(self, result: Any) -> None:
        self.bar.setValue(100)
        self._finish("完成", OK_COLOR)
        if self._on_success:
            self._on_success(result)

    def _done_error(self, message: str) -> None:
        self.bar.setValue(0)
        self._finish(f"失敗：{message}", ERROR_COLOR)

    def _done_cancelled(self) -> None:
        self.bar.setValue(0)
        self._finish("已取消", "gray")
