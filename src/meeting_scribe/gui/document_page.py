"""「產出文件」分頁：修正逐字稿文字、填寫會議資訊、選擇產出項目、產出文件。

逐字稿來源：
- 「轉錄」分頁轉錄完成時自動帶入並切換到本分頁；
- 「匯入逐字稿檔案…」：已經有的逐字稿（txt、srt、docx、json）；
- 「開啟已存的文件…」：之前在本分頁儲存的 document.json。

目前可以產出逐字稿 .docx；會議紀錄與心智圖需要 AI 整理討論事項與結論（第 2、3 階段完成後產出）。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol

from PySide6.QtCore import QDate, Qt, QTime
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from meeting_scribe.config import jobs_dir, output_dir
from meeting_scribe.gui.widgets import OK_COLOR, WARN_COLOR, RunPanel, hint, open_path, primary_button
from meeting_scribe.render.roc_date import format_roc_date, format_roc_time, parse_hhmm, round_to_minutes
from meeting_scribe.transcript_io import (
    DOCUMENT_FILE,
    MEETING_TYPES,
    EditHistory,
    ImportError_,
    MeetingInfo,
    TranscriptDocument,
    import_transcript,
)

TRANSCRIPT_FILTER = "逐字稿 (*.txt *.srt *.docx *.json);;所有檔案 (*.*)"
TYPE_HINTS = {
    "topic": "結論依主題歸納，長官裁示獨立成最後一項。",
    "qa": "討論事項條列簡報內容，結論依提問者分段（問題＋回覆要點）。",
    "agenda": "依議程逐案：各單位意見，主席裁示放在最後。建議在下方填寫議程。",
}


class DocumentHost(Protocol):
    """主視窗提供給本分頁的功能。"""

    cfg: dict[str, Any]

    def remember_meeting_fields(self, location: str, chair: str, recorder: str) -> None:
        """記住最近的地點、主席、紀錄。"""

    def statusBar(self) -> Any:
        """狀態列。"""


def _hms(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


class DocumentPage(QWidget):
    """「產出文件」分頁。"""

    def __init__(self, host: DocumentHost) -> None:
        super().__init__()
        self.host = host
        self.doc: TranscriptDocument | None = None
        self.last_output: Path | None = None
        self._dirty = False
        self._loading = False
        self.history = EditHistory()
        self._opened: tuple[list[Any], dict[str, str]] | None = None  # 舊版文件沒有原本內容時，改用開啟時的內容

        body = QWidget()
        lay = QVBoxLayout(body)

        # ---- 來源 ----
        src = QHBoxLayout()
        self.source_label = QLabel()
        self.source_label.setWordWrap(True)
        import_btn = QPushButton("匯入逐字稿檔案…")
        import_btn.setToolTip("已經有逐字稿時使用：支援 txt、srt、docx，以及本程式的 json")
        import_btn.clicked.connect(self.import_file)
        open_btn = QPushButton("開啟已存的文件…")
        open_btn.setToolTip("開啟之前在本分頁儲存的文件（document.json）")
        open_btn.clicked.connect(self.open_saved)
        src.addWidget(self.source_label, 1)
        src.addWidget(import_btn)
        src.addWidget(open_btn)
        lay.addLayout(src)
        self.empty_hint = hint("還沒有逐字稿：到「轉錄」分頁轉錄錄音檔（完成後會自動切到這裡），"
                               "或按右上方「匯入逐字稿檔案…」使用已經有的逐字稿。")
        self.empty_hint.setStyleSheet("color: palette(placeholder-text); font-size: 11pt; padding: 12px;")
        lay.addWidget(self.empty_hint)

        # ---- 1. 逐字稿內容 ----
        self.transcript_box = QGroupBox("1. 逐字稿內容（可以直接修改）")
        v = QVBoxLayout(self.transcript_box)
        v.addWidget(hint("按兩下「內容」修正錯字；「講者」可以改成單位名稱（例：公路局），只改這一段。"
                         "要一次改全部，請用「講者命名…」；同一人被拆成兩個講者時用「合併講者…」。"
                         "產出文件前可以按「AI 校正…」檢查錯字與講者身分（確認後才套用）。"
                         "改錯了可以按「↶ 復原」（Ctrl+Z）回到上一步，或「還原原本內容…」回到轉錄或匯入時的逐字稿。"))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["時間碼", "講者", "內容"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setMinimumHeight(240)
        self.table.itemChanged.connect(self._cell_changed)
        tools = QHBoxLayout()
        rename = QPushButton("講者命名…")
        rename.clicked.connect(self.rename_speaker)
        merge = QPushButton("合併講者…")
        merge.clicked.connect(self.merge_speakers)
        self.correct_button = QPushButton("AI 校正…")
        self.correct_button.setToolTip(
            "產出文件前檢查逐字稿：\n"
            "・文字：詞庫的常見誤寫，以及 AI 依前後文找出的同音錯字、專有名詞寫錯（例：獨架→毒駕）\n"
            "・講者角色：AI 依發言內容推測講者身分（例：講者1→主席）\n"
            "整份跑完後列出建議，由您勾選確認後才套用；套用後可以按「↶ 復原」。")
        self.correct_button.clicked.connect(self.ai_correct)
        self.count_label = hint("")
        self.undo_button = QPushButton("↶ 復原")
        self.undo_button.clicked.connect(self.undo)
        self.restore_button = QPushButton("還原原本內容…")
        self.restore_button.clicked.connect(self.restore_original)
        tools.addWidget(rename)
        tools.addWidget(merge)
        tools.addWidget(self.correct_button)
        tools.addSpacing(16)
        tools.addWidget(self.undo_button)
        tools.addWidget(self.restore_button)
        tools.addStretch(1)
        sc = QShortcut(QKeySequence(QKeySequence.StandardKey.Undo), self.transcript_box, activated=self.undo)
        sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._update_history_buttons()
        tools.addWidget(self.count_label)
        v.addWidget(self.table, 1)
        v.addLayout(tools)
        lay.addWidget(self.transcript_box)

        # ---- 2. 會議資訊 ----
        self.info_box = QGroupBox("2. 會議資訊（會議紀錄表頭）")
        form = QFormLayout(self.info_box)
        self.title = QPlainTextEdit()
        self.title.setPlaceholderText("可以兩行，例：\n道路交通安全推動計畫\n第2次研商會議紀錄")
        self.title.setFixedHeight(58)
        self.date = QDateEdit()
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("yyyy-MM-dd")
        self.date_label = hint("")
        self.start = QTimeEdit()
        self.start.setDisplayFormat("HH:mm")
        self.start_label = hint("")
        self.end = QTimeEdit()
        self.end.setDisplayFormat("HH:mm")
        self.end_label = hint("")
        self.location = QComboBox()
        self.location.setEditable(True)
        self.chair = QComboBox()
        self.chair.setEditable(True)
        self.recorder = QLineEdit()
        self.attendees = QLineEdit()
        self.agenda = QPlainTextEdit()
        self.agenda.setPlaceholderText("選填，一行一案。議程逐項式建議填寫；沒填時由 AI 自行歸納議題。")
        self.agenda.setFixedHeight(70)
        form.addRow("會議名稱 *：", self.title)
        form.addRow("會議日期 *：", self._with_label(self.date, self.date_label))
        form.addRow("開始時間 *：", self._with_label(self.start, self.start_label))
        form.addRow("散會時間：", self._with_label(self.end, self.end_label))
        form.addRow("會議地點：", self.location)
        form.addRow("主席：", self.chair)
        form.addRow("紀錄：", self.recorder)
        form.addRow("參加人員：", self.attendees)
        form.addRow("議程：", self.agenda)
        lay.addWidget(self.info_box)

        # ---- 3. 產出項目 ----
        self.output_box = QGroupBox("3. 產出項目")
        v = QVBoxLayout(self.output_box)
        v.addWidget(QLabel("會議類型（決定結論的整理方式）："))
        self.type_group = QButtonGroup(self)
        self.type_buttons: dict[str, QRadioButton] = {}
        for code, label in MEETING_TYPES.items():
            rb = QRadioButton(label)
            rb.setToolTip(TYPE_HINTS[code])
            self.type_group.addButton(rb)
            self.type_buttons[code] = rb
            row = QHBoxLayout()
            row.addSpacing(12)
            row.addWidget(rb)
            row.addWidget(hint(TYPE_HINTS[code]), 1)
            v.addLayout(row)
        self.type_hint = hint("")
        self.type_hint.hide()
        self.items_label = QLabel("要產出的項目（至少選一項）：")
        v.addWidget(self.items_label)
        items = QHBoxLayout()
        items.addSpacing(12)
        self.want_transcript = QCheckBox("逐字稿 .docx")
        self.want_transcript.setChecked(True)
        self.want_discussion = QCheckBox("討論事項")
        self.want_conclusion = QCheckBox("結論")
        self.want_action_table = QCheckBox("待辦事項表")
        self.want_mindmap = QCheckBox("心智圖（PNG、XMind）")
        self.output_checks = (self.want_transcript, self.want_discussion, self.want_conclusion,
                              self.want_action_table, self.want_mindmap)
        for cb in self.output_checks:
            items.addWidget(cb)
            cb.toggled.connect(self._check_outputs)
        items.addStretch(1)
        v.addLayout(items)
        self.none_warning = QLabel("請至少勾選一個要產出的項目。")
        self.none_warning.setStyleSheet(f"color: {WARN_COLOR}; font-weight: bold; padding-left: 12px;")
        self.none_warning.hide()
        v.addWidget(self.none_warning)
        v.addWidget(hint("勾選討論事項、結論、待辦事項表或心智圖時，會由 AI 依會議類型整理（「整理討論事項、結論、待辦」模型），"
                         "每條結論以 Word 註解標明依據時間碼，找不到依據的以黃色標「（待查證）」；心智圖輸出 PNG 與 XMind。"))
        lay.addWidget(self.output_box)

        # ---- 4. 產出前的文字處理（與產出項目分開：這是處理方式，不是產出的檔案） ----
        self.process_box = QGroupBox("4. 產出前的文字處理")
        pv = QVBoxLayout(self.process_box)
        card = QWidget()
        card.setObjectName("cleanCard")
        card.setStyleSheet(
            "#cleanCard { background: rgba(31, 111, 235, 0.07); border-left: 4px solid #1f6feb;"
            " border-radius: 6px; }"
        )
        cv = QVBoxLayout(card)
        cv.setContentsMargins(14, 10, 12, 10)
        self.want_clean = QCheckBox("刪除贅字")
        self.want_clean.setChecked(True)
        self.want_clean.setStyleSheet("QCheckBox { font-weight: bold; font-size: 11pt; }")
        self.want_clean.setToolTip("只影響產出的檔案，上方表格裡的原文不會被改。")
        cv.addWidget(self.want_clean)
        for line in (
            "會刪除：呃、嗯、欸、那個（停頓時）、對對對 → 對、重複的字（我我我們 → 我們）",
            "只刪不改：AI 的結果必須只少字、不能加字或換順序，沒通過的段落改用規則刪除的結果",
            "只影響產出的檔案（逐字稿與 AI 整理用的文字），上方表格裡的原文不會被改",
            "使用「刪除贅字」模型（預設 Haiku 5.5，每場約 US$0.03，可在「API 金鑰」分頁更改）",
        ):
            row = QHBoxLayout()
            row.setContentsMargins(24, 0, 0, 0)
            row.addWidget(hint("・" + line), 1)
            cv.addLayout(row)
        pv.addWidget(card)
        lay.addWidget(self.process_box)

        # ---- 產出 ----
        run = QHBoxLayout()
        self.folder_label = hint("")
        self.save_button = QPushButton("儲存修改")
        self.save_button.setToolTip("把修正的文字與會議資訊存起來（document.json），之後可以「開啟已存的文件」繼續")
        self.save_button.clicked.connect(lambda: self.save(quiet=False))
        self.open_folder = QPushButton("開啟這場會議的資料夾")
        self.open_folder.clicked.connect(self._open_folder)
        self.open_file = QPushButton("開啟逐字稿")
        self.open_file.clicked.connect(lambda: self.last_output and open_path(self.last_output))
        self.last_minutes: Path | None = None
        self.last_mindmap: Path | None = None
        self.open_minutes = QPushButton("開啟會議紀錄")
        self.open_minutes.setToolTip("每條結論都有 Word 註解標明依據時間碼；黃色為待查證")
        self.open_minutes.setEnabled(False)
        self.open_minutes.clicked.connect(lambda: self.last_minutes and open_path(self.last_minutes))
        self.open_mindmap = QPushButton("開啟心智圖")
        self.open_mindmap.setEnabled(False)
        self.open_mindmap.clicked.connect(lambda: self.last_mindmap and open_path(self.last_mindmap))
        self.produce_button = primary_button("產出文件")
        self.produce_button.clicked.connect(self.produce)
        run.addStretch(1)
        for b in (self.save_button, self.open_folder, self.open_file, self.open_minutes, self.open_mindmap,
                  self.produce_button):
            run.addWidget(b)
        self.folder_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.folder_label)
        lay.addLayout(run)
        self.result_label = QLabel("")
        self.result_label.setWordWrap(True)
        lay.addWidget(self.result_label)
        # 執行紀錄：目前步驟、進度、已經過時間、逐行紀錄（與「轉錄」分頁相同）
        self.panel = RunPanel()
        self.panel.log.setMinimumHeight(140)
        self.panel.busy_changed.connect(self._on_busy)
        lay.addWidget(self.panel)
        lay.addStretch(1)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # 內容一律配合視窗寬度
        area.setWidget(body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(area)

        self.date.dateChanged.connect(self._update_labels)
        self.start.timeChanged.connect(self._update_labels)
        self.end.timeChanged.connect(self._update_labels)
        self.type_group.buttonToggled.connect(lambda *_: self._update_labels())
        for w in (self.title, self.agenda):
            w.textChanged.connect(self._mark_dirty)
        self.title.textChanged.connect(self._update_labels)  # 輸出資料夾名稱跟著會議名稱變
        for w in (self.recorder, self.attendees):
            w.textChanged.connect(self._mark_dirty)
        for w in (self.location, self.chair):
            w.currentTextChanged.connect(self._mark_dirty)
        self.date.dateChanged.connect(self._mark_dirty)
        self.start.timeChanged.connect(self._mark_dirty)
        self.end.timeChanged.connect(self._mark_dirty)
        for cb in (*self.output_checks, self.want_clean):
            cb.toggled.connect(self._mark_dirty)
        self.type_group.buttonToggled.connect(lambda *_: self._mark_dirty())
        self._set_enabled(False)

    # ---- 小工具 ----
    @staticmethod
    def _with_label(widget: QWidget, label: QLabel) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(widget)
        row.addWidget(label, 1)
        return box

    def _set_enabled(self, on: bool) -> None:
        for w in (self.transcript_box, self.info_box, self.output_box, self.process_box, self.save_button,
                  self.produce_button):
            w.setEnabled(on)
        self.open_folder.setEnabled(on)
        if on:
            self._check_outputs()
        self.open_file.setEnabled(self.last_output is not None)
        self.empty_hint.setVisible(not on)
        if not on:
            self.source_label.setText("<b>目前沒有逐字稿</b>")

    def _mark_dirty(self, *_: Any) -> None:
        if not self._loading and self.doc is not None:
            self._dirty = True

    @property
    def dirty(self) -> bool:
        return self._dirty

    def _update_labels(self, *_: Any) -> None:
        d = self.date.date().toPython()
        self.date_label.setText(format_roc_date(d))
        self.start_label.setText(format_roc_time(self.start.time().toPython()))
        self.end_label.setText(f"散會({format_roc_time(self.end.time().toPython())})")
        code = self.meeting_type()
        self.type_hint.setText(TYPE_HINTS.get(code, ""))
        if self.doc is not None:
            self.folder_label.setText(f"產出的檔案會存在：{self.output_folder()}")

    def output_folder(self) -> Path:
        """這場會議的輸出資料夾：輸出資料夾\\{民國年月日}_{會議名稱}（依目前畫面上的名稱與日期）。"""
        from meeting_scribe import tasks

        info = MeetingInfo(title=self.title.toPlainText(), date=self.date.date().toPython().isoformat())
        return tasks.meeting_folder(output_dir(self.host.cfg), info)

    def meeting_type(self) -> str:
        return next((c for c, rb in self.type_buttons.items() if rb.isChecked()), "topic")

    # ---- 載入 ----
    def _confirm_discard(self) -> bool:
        if not self._dirty:
            return True
        answer = QMessageBox.question(
            self, "產出文件", "目前的逐字稿有尚未儲存的修改，要先儲存嗎？",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Save:
            return self.save(quiet=True)
        return True

    def load(self, doc: TranscriptDocument) -> None:
        """載入一份逐字稿文件。"""
        self._loading = True
        self.doc = doc
        self.last_output = None
        ui = self.host.cfg.get("ui", {})
        info = doc.info
        self.title.setPlainText(info.title)
        self.date.setDate(QDate.fromString(info.date, "yyyy-MM-dd"))
        start = parse_hhmm(info.start_time) or time(14, 0)
        self.start.setTime(QTime(start.hour, start.minute))
        end = parse_hhmm(info.end_time)
        if end is None:  # 散會時間預設＝開始時間＋錄音長度，四捨五入到 5 分鐘
            span = timedelta(seconds=doc.audio_duration or 0)
            end = round_to_minutes((datetime.combine(date.today(), start) + span).time())
        self.end.setTime(QTime(end.hour, end.minute))
        self._fill_recent(self.location, ui.get("recent_locations", []), info.location)
        self._fill_recent(self.chair, ui.get("recent_chairs", []), info.chair)
        self.recorder.setText(info.recorder or ui.get("last_recorder", ""))
        self.attendees.setText(info.attendees)
        self.agenda.setPlainText("\n".join(info.agenda))
        self.type_buttons.get(info.meeting_type, self.type_buttons["topic"]).setChecked(True)
        self.want_discussion.setChecked(info.want_discussion)
        self.want_conclusion.setChecked(info.want_conclusion)
        self.want_action_table.setChecked(info.want_action_table)
        self.want_mindmap.setChecked(info.want_mindmap)
        self.want_clean.setChecked(info.clean_fillers)
        self.want_transcript.setChecked(info.want_transcript)
        self.history.clear()
        self._opened = None if doc.has_original() else (list(doc.segments), dict(doc.speaker_names))
        self._fill_table()
        self._update_labels()
        self._update_history_buttons()
        extra = f"｜錄音 {_hms(doc.audio_duration)}" if doc.audio_duration and doc.has_timecodes else ""
        no_time = "｜沒有時間碼" if not doc.has_timecodes else ""
        self.source_label.setText(f"<b>來源：</b>{doc.source}{extra}{no_time}")
        self.result_label.setText("")
        self._set_enabled(True)
        self.panel.append(f"載入逐字稿：{doc.source}，{len(doc.segments)} 段、{len(doc.speakers())} 位講者")
        self._loading = False
        self._dirty = False

    @staticmethod
    def _fill_recent(combo: QComboBox, recent: list[str], value: str) -> None:
        combo.blockSignals(True)
        combo.clear()
        combo.addItems([r for r in recent if r])
        combo.setCurrentText(value)
        combo.blockSignals(False)

    def _fill_table(self) -> None:
        assert self.doc is not None
        self._loading = True
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.doc.segments))
        for row, seg in enumerate(self.doc.segments):
            stamp = _hms(seg.start) if self.doc.has_timecodes else "—"
            t = QTableWidgetItem(stamp)
            t.setFlags(t.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name = self.doc.display_name(seg.speaker) + ("（疑似）" if seg.speaker_uncertain else "")
            self.table.setItem(row, 0, t)
            self.table.setItem(row, 1, QTableWidgetItem(name))
            self.table.setItem(row, 2, QTableWidgetItem(seg.text))
        self.table.blockSignals(False)
        self.table.resizeRowsToContents()
        speakers = self.doc.speakers()
        self.count_label.setText(f"共 {len(self.doc.segments)} 段、{len(speakers)} 位講者")
        self._loading = False

    def import_file(self) -> None:
        """匯入已經有的逐字稿檔。"""
        if not self._confirm_discard():
            return
        chosen, _ = QFileDialog.getOpenFileName(self, "匯入逐字稿", str(Path.home()), TRANSCRIPT_FILTER)
        if not chosen:
            return
        path = Path(chosen)
        folder = jobs_dir() / f"{datetime.now():%Y%m%d_%H%M}_{path.stem}_匯入"
        try:
            doc = import_transcript(path, folder)
        except ImportError_ as exc:
            QMessageBox.warning(self, "匯入逐字稿", str(exc))
            return
        self.load(doc)
        self.host.statusBar().showMessage(f"已匯入 {len(doc.segments)} 段逐字稿：{path.name}", 8000)
        self.panel.append(f"匯入逐字稿：{path.name}（{'有' if doc.has_timecodes else '沒有'}時間碼）")

    def open_saved(self) -> None:
        """開啟之前存的 document.json。"""
        if not self._confirm_discard():
            return
        start = str(jobs_dir())
        chosen, _ = QFileDialog.getOpenFileName(self, "開啟已存的文件", start, f"文件 ({DOCUMENT_FILE})")
        if not chosen:
            return
        try:
            doc = TranscriptDocument.load(Path(chosen))
        except Exception as exc:  # noqa: BLE001 - 顯示給使用者
            QMessageBox.warning(self, "開啟文件", f"無法開啟：{exc}")
            return
        self.load(doc)

    # ---- 編輯 ----
    def _cell_changed(self, item: QTableWidgetItem) -> None:
        if self._loading or self.doc is None:
            return
        row, col = item.row(), item.column()
        seg = self.doc.segments[row]
        text = item.text().strip()
        stamp = _hms(seg.start) if self.doc.has_timecodes else f"第 {row + 1} 段"
        if col == 2 and text != seg.text:
            self.history.record(self.doc, f"修改 {stamp} 的內容")
            self.doc.segments[row] = seg.model_copy(update={"text": text})
        elif col == 1:
            name = text.removesuffix("（疑似）").strip()
            speaker = self._speaker_from_name(name) if name else seg.speaker
            if speaker == seg.speaker:
                return
            self.history.record(self.doc, f"把 {stamp} 的講者改成「{name}」")  # 改單一片段的講者
            self.doc.segments[row] = seg.model_copy(update={"speaker": speaker, "speaker_uncertain": False})
            self._fill_table()
        else:
            return
        self.doc.edited = True
        self._dirty = True
        self._update_history_buttons()

    def _speaker_from_name(self, name: str) -> str:
        """輸入的名稱若是既有講者（代號或命名）就沿用該代號，否則當成新講者。"""
        assert self.doc is not None
        for spk in self.doc.speakers():
            if name in (spk, self.doc.display_name(spk)):
                return spk
        return name

    def _choose_speaker(self, title: str, prompt: str, exclude: str | None = None) -> str | None:
        assert self.doc is not None
        speakers = [s for s in self.doc.speakers() if s != exclude]
        labels = [f"{self.doc.display_name(s)}" + (f"（{s}）" if self.doc.display_name(s) != s else "")
                  for s in speakers]
        if not labels:
            return None
        choice, ok = QInputDialog.getItem(self, title, prompt, labels, 0, False)
        return speakers[labels.index(choice)] if ok and choice in labels else None

    def rename_speaker(self) -> None:
        """講者命名：所有片段同步更新。"""
        if self.doc is None:
            return
        speaker = self._choose_speaker("講者命名", "要命名哪一位講者？")
        if speaker is None:
            return
        name, ok = QInputDialog.getText(self, "講者命名", f"「{speaker}」改成（例：公路局；留空表示取消命名）：",
                                        text=self.doc.speaker_names.get(speaker, ""))
        if not ok or name.strip() == self.doc.speaker_names.get(speaker, ""):
            return
        self.history.record(self.doc, f"講者命名：{self.doc.display_name(speaker)} → {name.strip() or speaker}")
        self.doc.rename_speaker(speaker, name)
        self._update_history_buttons()
        self.panel.append(f"講者命名：{speaker} → {name.strip() or '（取消命名）'}")
        self._fill_table()
        self._dirty = True

    def merge_speakers(self) -> None:
        """合併講者：同一人被拆成兩個講者時使用。"""
        if self.doc is None or len(self.doc.speakers()) < 2:
            QMessageBox.information(self, "合併講者", "至少要有兩位講者才能合併。")
            return
        source = self._choose_speaker("合併講者", "要把哪一位講者併入別人？")
        if source is None:
            return
        target = self._choose_speaker("合併講者", f"把「{self.doc.display_name(source)}」併入：", exclude=source)
        if target is None:
            return
        label = f"合併講者：{self.doc.display_name(source)} 併入 {self.doc.display_name(target)}"
        self.history.record(self.doc, label)
        count = self.doc.merge_speaker(source, target)
        self._update_history_buttons()
        self._fill_table()
        self._dirty = True
        self.host.statusBar().showMessage(f"已合併 {count} 段", 5000)
        self.panel.append(f"合併講者：{source} 併入 {target}（{count} 段）")

    # ---- AI 校正（語意與講者角色） ----
    def ai_correct(self) -> None:
        """整份逐字稿在背景跑完 AI 校正後，開啟確認視窗；使用者勾選的建議才套用。"""
        if self.doc is None or self.panel.running:
            return
        doc = self.doc.model_copy(deep=True)  # 背景處理用當下的內容
        cfg = self.host.cfg

        def work(progress: Any, cancel: Any) -> Any:
            from meeting_scribe import tasks

            return tasks.suggest_corrections(doc, cfg, progress, cancel)

        self.panel.start("AI 校正", work, self._corrections_ready)

    def _open_correction_dialog(self, suggestions: Any) -> tuple[list[Any], list[Any]] | None:
        """顯示確認視窗，回傳勾選的（文字校正, 講者角色）；取消時回傳 None。"""
        from meeting_scribe.gui.correction_dialog import CorrectionDialog

        dialog = CorrectionDialog(self, self.doc, suggestions)
        return dialog.chosen() if dialog.exec() == CorrectionDialog.DialogCode.Accepted else None

    def _corrections_ready(self, suggestions: Any) -> None:
        from meeting_scribe.postprocess.semantic import apply_suggestions

        if self.doc is None:
            return
        for note in suggestions.notes:
            self.panel.append(note)
        if suggestions.empty():
            QMessageBox.information(self, "AI 校正", "沒有發現需要校正的地方。"
                                    + ("\n\n" + "\n".join(suggestions.notes) if suggestions.notes else ""))
            return
        chosen = self._open_correction_dialog(suggestions)
        if chosen is None:
            self.panel.append("AI 校正：沒有套用任何建議")
            return
        fixes, roles = chosen
        if not fixes and not roles:
            self.panel.append("AI 校正：沒有勾選任何建議")
            return
        self.history.record(self.doc, f"AI 校正（{len(fixes)} 處文字、{len(roles)} 位講者）")
        applied, named = apply_suggestions(self.doc, fixes, roles)
        skipped = len(fixes) - applied
        label = f"{applied} 處文字、{named} 位講者" + (f"（{skipped} 處原文已改過，略過）" if skipped else "")
        self._after_history_change("已套用 AI 校正", label)

    # ---- 復原、還原原本內容 ----
    def _update_history_buttons(self) -> None:
        editable = self.doc is not None and not getattr(self, "_busy", False)
        undo = self.history.undo_label
        self.undo_button.setEnabled(editable and undo is not None)
        self.restore_button.setEnabled(editable)
        self.correct_button.setEnabled(editable)
        self.undo_button.setToolTip(f"復原：{undo}（Ctrl+Z）" if undo else "沒有可以復原的修改")
        self.restore_button.setToolTip("逐字稿內容與講者回到原本（轉錄或匯入時）的樣子；會議資訊不受影響。"
                                       "還原後反悔可以按「↶ 復原」")

    def _after_history_change(self, action: str, label: str) -> None:
        self._dirty = True
        self._fill_table()
        self._update_history_buttons()
        self.panel.append(f"{action}：{label}")
        self.host.statusBar().showMessage(f"{action}：{label}", 5000)

    def undo(self) -> None:
        """回到上一步（Ctrl+Z）。"""
        if self.doc is None or self.table.state() == QAbstractItemView.State.EditingState:
            return  # 正在編輯儲存格時，Ctrl+Z 交給儲存格自己處理
        label = self.history.undo(self.doc)
        if label:
            self._after_history_change("已復原", label)

    def restore_original(self) -> None:
        """還原原本內容：逐字稿與講者回到轉錄或匯入時的樣子（還原後可以按「復原」反悔）。"""
        if self.doc is None:
            return
        if self.doc.has_original():
            what = "原本（轉錄或匯入時）"
        else:
            what = "這次開啟時（這份文件是舊版存的，沒有原本內容的紀錄）"
        answer = QMessageBox.question(
            self, "還原原本內容",
            f"要把逐字稿內容與講者還原成{what}的樣子嗎？\n\n"
            "・所有文字修改、講者命名、合併講者都會取消\n"
            "・會議資訊與產出選項不受影響\n"
            "・還原後如果反悔，可以按「↶ 復原」回來")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.history.record(self.doc, "還原原本內容")
        if not self.doc.restore_original() and self._opened is not None:
            self.doc.segments, self.doc.speaker_names = list(self._opened[0]), dict(self._opened[1])
        self._after_history_change("已還原", "逐字稿回到" + ("原本的內容" if self.doc.has_original() else "開啟時的內容"))

    # ---- 儲存與產出 ----
    def collect_info(self) -> MeetingInfo:
        """把畫面上的會議資訊整理成 MeetingInfo。"""
        return MeetingInfo(
            title=self.title.toPlainText().strip(),
            date=self.date.date().toPython().isoformat(),
            start_time=self.start.time().toString("HH:mm"),
            end_time=self.end.time().toString("HH:mm"),
            location=self.location.currentText().strip(),
            chair=self.chair.currentText().strip(),
            recorder=self.recorder.text().strip(),
            attendees=self.attendees.text().strip(),
            agenda=[ln.strip() for ln in self.agenda.toPlainText().splitlines() if ln.strip()],
            meeting_type=self.meeting_type(),  # type: ignore[arg-type]
            want_discussion=self.want_discussion.isChecked(),
            want_conclusion=self.want_conclusion.isChecked(),
            want_action_table=self.want_action_table.isChecked(),
            want_mindmap=self.want_mindmap.isChecked(),
            clean_fillers=self.want_clean.isChecked(),
            want_transcript=self.want_transcript.isChecked(),
        )

    def save(self, quiet: bool = True) -> bool:
        """存成 document.json。"""
        if self.doc is None:
            return False
        self.doc.info = self.collect_info()
        try:
            path = self.doc.save()
        except OSError as exc:
            QMessageBox.critical(self, "儲存修改", f"無法儲存：{exc}")
            return False
        self._dirty = False
        info = self.doc.info
        self.host.remember_meeting_fields(info.location, info.chair, info.recorder)
        self.host.statusBar().showMessage(f"已儲存：{path}", 6000)
        self.panel.append(f"已儲存修改：{path.name}")
        if not quiet:
            QMessageBox.information(self, "儲存修改", f"已儲存，之後可以用「開啟已存的文件…」繼續：\n{path}")
        return True

    def produce(self) -> None:
        """產出文件（在背景執行，進度與經過時間顯示在下方執行紀錄）：目前產出逐字稿 .docx。"""
        from meeting_scribe.render.docx_transcript import render_transcript

        if self.doc is None or self.panel.running:
            return
        if not self._check_outputs():
            QMessageBox.information(self, "產出文件", "請至少勾選一個要產出的項目。")
            return
        if not self.title.toPlainText().strip():
            self.title.setFocus()
            QMessageBox.information(self, "產出文件", "請先填寫會議名稱。")
            return
        if not self.save(quiet=True):
            return
        doc = self.doc.model_copy(deep=True)  # 背景產出時使用當下的內容，不受之後的編輯影響
        out_dir = self.output_folder()
        want_transcript = self.want_transcript.isChecked()
        want_ai = any(cb.isChecked() for cb in (self.want_discussion, self.want_conclusion,
                                                self.want_action_table, self.want_mindmap))
        self.result_label.setText("")
        clean = self.want_clean.isChecked()
        cfg = self.host.cfg
        registry = self.host.registry

        def work(progress: Any, cancel: Any) -> dict[str, Any]:
            from meeting_scribe import tasks
            from meeting_scribe.llm.base import LLMError
            from meeting_scribe.summarize.summarizer import SummaryError

            progress(0, f"已儲存會議資訊與修改（{len(doc.segments)} 段、{len(doc.speakers())} 位講者）")
            pairs = list(enumerate(doc.segments))  # （原始位置, 片段）：摘要依據編號對應原始位置
            if clean:
                before = sum(len(s.text) for s in doc.segments)
                cleaned, rep = tasks.clean_fillers(doc.segments, cfg, registry, progress, cancel,
                                                   Path(doc.folder) / "_work" / "filler.json")
                pairs = list(zip(rep.kept_indices, cleaned, strict=True))
                after = sum(len(s.text) for s in cleaned)
                detail = (f"AI 處理 {rep.ai_batches} 批" + (f"、沿用先前 {rep.reused_batches} 批" if rep.reused_batches else "")
                          + (f"、{rep.ai_rejected} 段 AI 結果未通過驗證改用規則" if rep.ai_rejected else "")
                          if rep.ai_used else "只用規則")
                progress(0, f"刪除贅字完成：{before:,} 字 → {after:,} 字（少了 {before - after:,} 字）；"
                            f"{detail}；整段都是贅字而移除 {rep.dropped} 段")
                if rep.ai_error:
                    progress(0, f"AI 刪除贅字中斷（{rep.ai_error}），其餘段落已用規則處理；"
                                "補足後再按「產出文件」，已完成的批次不會重複計費")
            result: dict[str, Any] = {"minutes": None, "minutes_error": ""}
            if want_ai:
                try:
                    result["minutes"] = tasks.summarize_meeting(doc, pairs, cfg, out_dir, progress, cancel)
                except (SummaryError, LLMError) as exc:
                    result["minutes_error"] = str(exc)
                    setup_issue = any(k in str(exc) for k in ("需要 AI", "已達上限"))
                    tail = ("逐字稿照常產出" if setup_issue else
                            "逐字稿照常產出；已完成的部分有存檔，再按「產出文件」會從中斷處繼續，不重複計費")
                    progress(100, f"AI 整理會議紀錄未完成：{exc}（{tail}）")
            result["transcript"] = None
            if want_transcript:
                clean_doc = doc.model_copy(update={"segments": [s for _, s in pairs]})
                result["transcript"] = render_transcript(clean_doc, out_dir, progress=progress)
            return result

        self.panel.start("產出文件", work, self._produced)

    def _produced(self, result: dict[str, Any]) -> None:
        path = result["transcript"]
        lines: list[str] = []
        if path is not None:
            self.last_output = path
            self.open_file.setEnabled(True)
            self.panel.append(f"輸出檔案：{path}")
            lines.append(f"<span style='color:{OK_COLOR}'><b>✔ 已產出逐字稿：</b></span>{path.name}")
        minutes = result.get("minutes")
        if minutes is not None:
            v = minutes.validation
            if minutes.minutes_path:
                self.last_minutes = minutes.minutes_path
                self.open_minutes.setEnabled(True)
                lines.append(f"<span style='color:{OK_COLOR}'><b>✔ 已產出會議紀錄：</b></span>"
                             f"{minutes.minutes_path.name}（{v.items} 條，{v.pending} 條待查證）")
            if minutes.png_path:
                self.last_mindmap = minutes.png_path
                self.open_mindmap.setEnabled(True)
                lines.append(f"<span style='color:{OK_COLOR}'><b>✔ 已產出心智圖：</b></span>"
                             f"{minutes.png_path.name}、{minutes.xmind_path.name if minutes.xmind_path else ''}")
            for p in minutes.files():
                self.panel.append(f"輸出檔案：{p}")
        elif result.get("minutes_error"):
            lines.append(f"<span style='color:{WARN_COLOR}'><b>會議紀錄未完成：</b></span>{result['minutes_error']}")
        self.result_label.setText("<br>".join(lines))
        self.host.statusBar().showMessage("產出完成：" + "、".join(
            p.name for p in ([path] if path else []) + (minutes.files() if minutes else [])), 8000)

    def _check_outputs(self, *_: Any) -> bool:
        """至少要勾選一個產出項目；都沒勾時顯示提醒並停用「產出文件」。"""
        any_checked = any(cb.isChecked() for cb in self.output_checks)
        self.none_warning.setVisible(not any_checked)
        self.produce_button.setEnabled(any_checked and self.doc is not None and not self.panel.running)
        self.produce_button.setToolTip("" if any_checked else "請至少勾選一個要產出的項目")
        return any_checked

    def _on_busy(self, busy: bool) -> None:
        """產出期間鎖住編輯與按鈕，避免內容在產出途中被改動。"""
        self._busy = busy
        for w in (self.transcript_box, self.info_box, self.output_box, self.process_box, self.save_button,
                  self.produce_button):
            w.setEnabled(not busy and self.doc is not None)
        if not busy:
            self._check_outputs()
        self._update_history_buttons()

    def _open_folder(self) -> None:
        if self.doc is None:
            return
        folder = self.output_folder()
        folder.mkdir(parents=True, exist_ok=True)
        open_path(folder)

