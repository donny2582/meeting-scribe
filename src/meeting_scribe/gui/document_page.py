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

from meeting_scribe.config import output_dir
from meeting_scribe.gui.widgets import OK_COLOR, RunPanel, hint, open_path, primary_button
from meeting_scribe.render.roc_date import format_roc_date, format_roc_time, parse_hhmm, round_to_minutes
from meeting_scribe.transcript_io import (
    DOCUMENT_FILE,
    MEETING_TYPES,
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
                         "要一次改全部，請用「講者命名…」；同一人被拆成兩個講者時用「合併講者…」。"))
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
        self.count_label = hint("")
        tools.addWidget(rename)
        tools.addWidget(merge)
        tools.addStretch(1)
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
        v.addWidget(QLabel("要產出的項目："))
        items = QHBoxLayout()
        items.addSpacing(12)
        self.want_transcript = QCheckBox("逐字稿 .docx（一律產出）")
        self.want_transcript.setChecked(True)
        self.want_transcript.setEnabled(False)
        self.want_discussion = QCheckBox("討論事項")
        self.want_conclusion = QCheckBox("結論")
        self.want_action_table = QCheckBox("待辦事項表")
        self.want_mindmap = QCheckBox("心智圖（PNG、XMind）")
        for cb in (self.want_transcript, self.want_discussion, self.want_conclusion,
                   self.want_action_table, self.want_mindmap):
            items.addWidget(cb)
        items.addStretch(1)
        v.addLayout(items)
        v.addWidget(hint("會議紀錄（討論事項、結論、待辦事項表）與心智圖需要 AI 整理討論內容，"
                         "第 2、3 階段完成後產出；目前先產出逐字稿 .docx，這裡的選擇會先存起來。"))
        lay.addWidget(self.output_box)

        # ---- 產出 ----
        run = QHBoxLayout()
        self.folder_label = hint("")
        self.save_button = QPushButton("儲存修改")
        self.save_button.setToolTip("把修正的文字與會議資訊存起來（document.json），之後可以「開啟已存的文件」繼續")
        self.save_button.clicked.connect(lambda: self.save(quiet=False))
        self.open_folder = QPushButton("開啟輸出資料夾")
        self.open_folder.clicked.connect(self._open_folder)
        self.open_file = QPushButton("開啟逐字稿")
        self.open_file.clicked.connect(lambda: self.last_output and open_path(self.last_output))
        self.produce_button = primary_button("產出文件")
        self.produce_button.clicked.connect(self.produce)
        run.addStretch(1)
        for b in (self.save_button, self.open_folder, self.open_file, self.produce_button):
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
        for w in (self.recorder, self.attendees):
            w.textChanged.connect(self._mark_dirty)
        for w in (self.location, self.chair):
            w.currentTextChanged.connect(self._mark_dirty)
        self.date.dateChanged.connect(self._mark_dirty)
        self.start.timeChanged.connect(self._mark_dirty)
        self.end.timeChanged.connect(self._mark_dirty)
        for cb in (self.want_discussion, self.want_conclusion, self.want_action_table, self.want_mindmap):
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
        for w in (self.transcript_box, self.info_box, self.output_box, self.save_button, self.produce_button):
            w.setEnabled(on)
        self.open_folder.setEnabled(on)
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
        self._fill_table()
        self._update_labels()
        extra = f"｜錄音 {_hms(doc.audio_duration)}" if doc.audio_duration and doc.has_timecodes else ""
        no_time = "｜沒有時間碼" if not doc.has_timecodes else ""
        self.source_label.setText(f"<b>來源：</b>{doc.source}{extra}{no_time}")
        self.folder_label.setText(f"輸出位置：{Path(doc.folder) / '輸出'}")
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
        folder = output_dir(self.host.cfg) / f"{datetime.now():%Y%m%d_%H%M}_{path.stem}_匯入"
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
        start = str(output_dir(self.host.cfg))
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
        if col == 2 and text != seg.text:
            self.doc.segments[row] = seg.model_copy(update={"text": text})
        elif col == 1:
            name = text.removesuffix("（疑似）").strip()
            speaker = self._speaker_from_name(name) if name else seg.speaker
            if speaker != seg.speaker:  # 改單一片段的講者
                self.doc.segments[row] = seg.model_copy(update={"speaker": speaker, "speaker_uncertain": False})
                self._fill_table()
        else:
            return
        self.doc.edited = True
        self._dirty = True

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
        if not ok:
            return
        self.doc.rename_speaker(speaker, name)
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
        count = self.doc.merge_speaker(source, target)
        self._fill_table()
        self._dirty = True
        self.host.statusBar().showMessage(f"已合併 {count} 段", 5000)
        self.panel.append(f"合併講者：{source} 併入 {target}（{count} 段）")

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
        if not self.title.toPlainText().strip():
            self.title.setFocus()
            QMessageBox.information(self, "產出文件", "請先填寫會議名稱。")
            return
        if not self.save(quiet=True):
            return
        doc = self.doc.model_copy(deep=True)  # 背景產出時使用當下的內容，不受之後的編輯影響
        out_dir = Path(doc.folder) / "輸出"
        pending = [name for name, on in (("會議紀錄（討論事項、結論、待辦事項表）", self.want_discussion.isChecked()
                                          or self.want_conclusion.isChecked() or self.want_action_table.isChecked()),
                                         ("心智圖", self.want_mindmap.isChecked())) if on]
        self.result_label.setText("")

        def work(progress: Any, cancel: Any) -> Path:
            progress(0, f"已儲存會議資訊與修改（{len(doc.segments)} 段、{len(doc.speakers())} 位講者）")
            path = render_transcript(doc, out_dir, progress=progress)
            for name in pending:
                progress(100, f"{name}：需要 AI 整理，第 2、3 階段完成後產出（本次略過）")
            return path

        self.panel.start("產出文件", work, self._produced)

    def _produced(self, path: Path) -> None:
        self.last_output = path
        self.open_file.setEnabled(True)
        self.panel.append(f"輸出檔案：{path}")
        self.result_label.setText(f"<span style='color:{OK_COLOR}'><b>✔ 已產出逐字稿：</b></span>{path.name}")
        self.host.statusBar().showMessage(f"已產出：{path}", 8000)

    def _on_busy(self, busy: bool) -> None:
        """產出期間鎖住編輯與按鈕，避免內容在產出途中被改動。"""
        for w in (self.transcript_box, self.info_box, self.output_box, self.save_button, self.produce_button):
            w.setEnabled(not busy and self.doc is not None)

    def _open_folder(self) -> None:
        if self.doc is None:
            return
        folder = Path(self.doc.folder) / "輸出"
        folder.mkdir(parents=True, exist_ok=True)
        open_path(folder)

