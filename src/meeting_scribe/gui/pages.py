"""工作分頁：「轉錄」「引擎評估」「詞庫」。

每個工作分頁的主要按鈕放在分頁下方；按下後由主視窗（host）先儲存設定並檢查金鑰，
再交給 :class:`RunPanel` 在背景執行，介面不會卡住。
"""

from __future__ import annotations

import csv
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from meeting_scribe import tasks
from meeting_scribe.config import default_glossary_names, user_glossary_dir
from meeting_scribe.gui.settings import ENGINES
from meeting_scribe.gui.widgets import (
    AUDIO_FILTER,
    CSV_FILTER,
    FilePicker,
    LanguagePicker,
    RunPanel,
    hint,
    open_path,
    primary_button,
)
from meeting_scribe.postprocess.glossary import COLUMNS, load_glossary
from meeting_scribe.postprocess.lang_detect import LanguageRegistry


class Host(Protocol):
    """主視窗提供給分頁的功能。"""

    registry: LanguageRegistry

    def prepare_run(self, engines: list[str]) -> dict[str, Any] | None:
        """儲存設定、檢查金鑰與 ffmpeg；可以執行時回傳最新設定，否則回傳 None。"""

    def go_to(self, page: str) -> None:
        """切換到指定分頁（transcribe／benchmark／glossary／keys／services）。"""

    def engines_changed(self) -> None:
        """使用者換了引擎選擇，需要更新金鑰提示。"""


def _scroll(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.setWidget(widget)
    return area


def _engine_combo(selected: str) -> QComboBox:
    combo = QComboBox()
    for code, label in ENGINES:
        combo.addItem(label, code)
    combo.setCurrentIndex(max(0, combo.findData(selected)))
    return combo


class GlossaryChecklist(QListWidget):
    """可勾選的詞庫清單（預設全部勾選）。"""

    def __init__(self) -> None:
        super().__init__()
        self.setMaximumHeight(70)
        self.reload()

    def reload(self) -> None:
        checked = {self.item(i).data(Qt.ItemDataRole.UserRole): self.item(i).checkState()
                   for i in range(self.count())}
        self.clear()
        for path in tasks.default_glossaries():
            item = QListWidgetItem(path.stem)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(checked.get(str(path), Qt.CheckState.Checked))
            self.addItem(item)

    def paths(self) -> list[Path]:
        return [Path(self.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.count())
                if self.item(i).checkState() == Qt.CheckState.Checked]


class TranscribePage(QWidget):
    """「轉錄」分頁：選錄音檔 → 設定 → 開始轉錄，輸出 JSON 與逐字稿草稿。"""

    def __init__(self, host: Host, cfg: dict[str, Any], output_dir: Path) -> None:
        super().__init__()
        self.host = host
        self.last_output: tasks.TranscribeOutput | None = None
        body = QWidget()
        lay = QVBoxLayout(body)

        box = QGroupBox("1. 錄音檔")
        v = QVBoxLayout(box)
        self.audio = FilePicker("open", AUDIO_FILTER, "選擇錄音檔，或直接把檔案拖到這裡", "選擇錄音檔")
        self.audio_info = hint("支援 mp3、m4a、wav、mp4（只取音軌）。")
        self.audio.changed.connect(self._update_audio_info)
        v.addWidget(self.audio)
        v.addWidget(self.audio_info)
        lay.addWidget(box)

        box = QGroupBox("2. 辨識設定")
        form = QFormLayout(box)
        self.engine = _engine_combo(cfg.get("stt", {}).get("default_engine", "azure"))
        self.engine.currentIndexChanged.connect(lambda _i: self.host.engines_changed())
        ui = cfg.get("ui", {})
        self.languages = LanguagePicker(host.registry, ui.get("last_languages")
                                        or cfg.get("languages", {}).get("default", ["zh", "en"]))
        self.speakers = QSpinBox()
        self.speakers.setRange(0, 36)
        self.speakers.setSpecialValueText("自動")
        self.speakers.setToolTip("提供給語者辨識參考；不確定就用「自動」。")
        self.glossaries = GlossaryChecklist()
        manage = QPushButton("管理詞庫…")
        manage.clicked.connect(lambda: self.host.go_to("glossary"))
        g_row = QHBoxLayout()
        g_row.addWidget(self.glossaries, 1)
        g_row.addWidget(manage, 0, Qt.AlignmentFlag.AlignTop)
        form.addRow("引擎：", self.engine)
        form.addRow("會議語言：", self.languages)
        form.addRow("預估講者人數：", self.speakers)
        form.addRow("套用詞庫：", g_row)
        lay.addWidget(box)

        box = QGroupBox("3. 輸出")
        v = QVBoxLayout(box)
        form = QFormLayout()
        self.output = FilePicker("dir", title="選擇輸出資料夾")
        self.output.set_path(output_dir)
        form.addRow("輸出資料夾：", self.output)
        v.addLayout(form)
        v.addWidget(hint("每次轉錄會在這裡建立一個「日期時間_檔名_引擎」資料夾，"
                         "內含逐字稿草稿（.txt）與引擎原始結果（stt_raw.json）。"))
        lay.addWidget(box)

        run_row = QHBoxLayout()
        self.open_txt = QPushButton("開啟逐字稿草稿")
        self.open_folder = QPushButton("開啟輸出資料夾")
        for b in (self.open_txt, self.open_folder):
            b.setEnabled(False)
        self.open_txt.clicked.connect(lambda: self.last_output and open_path(self.last_output.txt_path))
        self.open_folder.clicked.connect(lambda: self.last_output and open_path(self.last_output.folder))
        self.run_button = primary_button("開始轉錄")
        self.run_button.clicked.connect(self.start)
        run_row.addWidget(self.open_txt)
        run_row.addWidget(self.open_folder)
        run_row.addStretch(1)
        run_row.addWidget(self.run_button)
        lay.addLayout(run_row)
        self.panel = RunPanel()
        lay.addWidget(self.panel, 1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(_scroll(body))

    def _update_audio_info(self, _text: str) -> None:
        path = self.audio.path()
        if path and path.is_file():
            self.audio_info.setText(f"{path.name}（{path.stat().st_size / 1024 / 1024:.1f} MB）")
        else:
            self.audio_info.setText("支援 mp3、m4a、wav、mp4（只取音軌）。")

    def selected_engines(self) -> list[str]:
        return [str(self.engine.currentData())]

    def start(self) -> None:
        """檢查輸入後在背景轉錄。"""
        audio = self.audio.path()
        if not audio or not audio.is_file():
            QMessageBox.information(self, "開始轉錄", "請先選擇錄音檔。")
            return
        out_root = self.output.path()
        if not out_root:
            QMessageBox.information(self, "開始轉錄", "請選擇輸出資料夾。")
            return
        cfg = self.host.prepare_run(self.selected_engines())
        if cfg is None:
            return
        engine = str(self.engine.currentData())
        langs = self.languages.codes()
        glossaries = self.glossaries.paths()
        speakers = self.speakers.value() or None
        registry = self.host.registry

        def work(progress: Any, cancel: Any) -> tasks.TranscribeOutput:
            return tasks.run_transcribe_job(engine, audio, langs, glossaries, cfg, registry, out_root,
                                            speakers, progress, cancel)

        self.panel.start(f"轉錄 {audio.name}", work, self._done)

    def _done(self, out: tasks.TranscribeOutput) -> None:
        self.last_output = out
        self.open_txt.setEnabled(True)
        self.open_folder.setEnabled(True)
        r = out.result
        self.panel.append(f"共 {len(r.segments)} 個片段、{len({s.speaker for s in r.segments})} 位講者，"
                          f"處理 {r.elapsed_sec / 60:.1f} 分鐘。輸出：{out.folder}")


class BenchmarkPage(QWidget):
    """「引擎評估」分頁（第 0 階段）：準備標準答案 → 選擇標準答案 → 執行評估。"""

    def __init__(self, host: Host, cfg: dict[str, Any], output_dir: Path) -> None:
        super().__init__()
        self.host = host
        self.report_path: Path | None = None
        self.drafts: list[Path] = []
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.addWidget(hint("用同一段真實會議錄音比較各引擎的辨識品質、講者辨識、詞庫命中率、語言判定、"
                           "處理時間與費用，依結果決定預設引擎（SPEC 5.3）。"))

        box = QGroupBox("步驟 1：準備測試錄音與標準答案草稿")
        step1 = QVBoxLayout(box)
        form = QFormLayout()
        step1.addLayout(form)
        self.audio = FilePicker("open", AUDIO_FILTER, "約 10 分鐘的真實會議錄音", "選擇測試錄音")
        self.lang_audio = FilePicker("open", AUDIO_FILTER, "選填：約 3 分鐘、含日文或韓文發言", "選擇語言測試錄音")
        self.draft_engine = _engine_combo(cfg.get("stt", {}).get("default_engine", "azure"))
        self.draft_button = QPushButton("產生標準答案草稿")
        self.open_drafts = QPushButton("用 Excel 開啟草稿")
        self.open_drafts.setEnabled(False)
        self.open_drafts.clicked.connect(self._open_drafts)
        self.draft_button.clicked.connect(self.make_drafts)
        row = QHBoxLayout()
        row.addWidget(self.draft_engine)
        row.addWidget(self.draft_button)
        row.addWidget(self.open_drafts)
        row.addStretch(1)
        form.addRow("測試錄音：", self.audio)
        form.addRow("日韓測試錄音：", self.lang_audio)
        form.addRow("草稿使用：", row)
        step1.addWidget(hint("草稿由引擎自動產生；請在 Excel 逐列校正 text（文字）、speaker（講者）、"
                             "lang（zh／en／ja／ko）後另存為 CSV UTF-8，作為步驟 2 的標準答案。"))
        lay.addWidget(box)

        box = QGroupBox("步驟 2：選擇校正後的標準答案")
        form = QFormLayout(box)
        self.reference = FilePicker("open", CSV_FILTER, "校正後的標準答案 CSV", "選擇標準答案")
        self.lang_reference = FilePicker("open", CSV_FILTER, "選填：日韓測試錄音的標準答案", "選擇標準答案")
        form.addRow("標準答案：", self.reference)
        form.addRow("日韓標準答案：", self.lang_reference)
        lay.addWidget(box)

        box = QGroupBox("步驟 3：執行評估")
        form = QFormLayout(box)
        eng_row = QHBoxLayout()
        self.engine_boxes: dict[str, QCheckBox] = {}
        with_keys = [c for c, _ in ENGINES if not tasks.missing_keys([c])]
        for code, label in ENGINES:
            cb = QCheckBox(label)
            cb.setChecked(code in with_keys if with_keys else True)  # 預設勾選已設定金鑰的引擎
            cb.toggled.connect(lambda _on: self.host.engines_changed())
            self.engine_boxes[code] = cb
            eng_row.addWidget(cb)
        eng_row.addStretch(1)
        self.languages = LanguagePicker(host.registry, cfg.get("languages", {}).get("default", ["zh", "en"]))
        self.reuse = QCheckBox("沿用先前的轉錄結果（只重新計算指標，不再呼叫 API）")
        self.out_dir = FilePicker("dir", title="選擇評估輸出資料夾")
        self.out_dir.set_path(output_dir / "引擎評估")
        form.addRow("比較引擎：", eng_row)
        form.addRow("會議語言：", self.languages)
        form.addRow("", self.reuse)
        form.addRow("輸出資料夾：", self.out_dir)
        lay.addWidget(box)

        run_row = QHBoxLayout()
        self.open_report = QPushButton("開啟報告檔")
        self.open_report.setEnabled(False)
        self.open_report.clicked.connect(lambda: self.report_path and open_path(self.report_path))
        self.run_button = primary_button("開始評估")
        self.run_button.clicked.connect(self.start)
        run_row.addWidget(self.open_report)
        run_row.addStretch(1)
        run_row.addWidget(self.run_button)
        lay.addLayout(run_row)

        self.panel = RunPanel()
        self.report = QTextBrowser()
        self.report.setPlaceholderText("評估完成後，比較表會顯示在這裡。")
        self.report.setMinimumHeight(160)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.panel)
        split.addWidget(self.report)
        lay.addWidget(split, 1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(_scroll(body))

    def selected_engines(self) -> list[str]:
        return [c for c, cb in self.engine_boxes.items() if cb.isChecked()]

    def _out(self) -> Path | None:
        return self.out_dir.path()

    def make_drafts(self) -> None:
        """由引擎結果產生標準答案草稿。"""
        audio, lang_audio, out = self.audio.path(), self.lang_audio.path(), self._out()
        if not audio or not audio.is_file():
            QMessageBox.information(self, "產生草稿", "請先選擇測試錄音。")
            return
        if not out:
            QMessageBox.information(self, "產生草稿", "請選擇輸出資料夾。")
            return
        engine = str(self.draft_engine.currentData())
        cfg = self.host.prepare_run([engine])
        if cfg is None:
            return
        langs = self.languages.codes()
        registry = self.host.registry
        glossaries = tasks.default_glossaries()

        def work(progress: Any, cancel: Any) -> list[Path]:
            paths = [tasks.make_reference(audio, engine, langs, glossaries, cfg, registry,
                                          out / "reference_draft.csv", progress, cancel)]
            if lang_audio and lang_audio.is_file():
                paths.append(tasks.make_reference(lang_audio, engine, ["zh", "en", "ja", "ko"], glossaries, cfg,
                                                  registry, out / "reference_lang_draft.csv", progress, cancel))
            return paths

        self.panel.start("產生標準答案草稿", work, self._drafts_done)

    def _drafts_done(self, paths: list[Path]) -> None:
        self.drafts = paths
        self.open_drafts.setEnabled(True)
        for p in paths:
            self.panel.append(f"草稿：{p}")
        self.panel.append("請用 Excel 校正後另存，再到步驟 2 選擇。")

    def _open_drafts(self) -> None:
        for p in self.drafts:
            if p.exists():
                open_path(p)

    def start(self) -> None:
        """執行評估。"""
        audio, ref, out = self.audio.path(), self.reference.path(), self._out()
        lang_audio, lang_ref = self.lang_audio.path(), self.lang_reference.path()
        engines = self.selected_engines()
        problems = []
        if not audio or not audio.is_file():
            problems.append("步驟 1 的測試錄音")
        if not ref or not ref.is_file():
            problems.append("步驟 2 的標準答案")
        if lang_audio and not (lang_ref and lang_ref.is_file()):
            problems.append("日韓測試錄音的標準答案（或清除日韓測試錄音）")
        if not engines:
            problems.append("至少一個比較引擎")
        if not out:
            problems.append("輸出資料夾")
        if problems:
            QMessageBox.information(self, "開始評估", "請先提供：\n・" + "\n・".join(problems))
            return
        reuse = self.reuse.isChecked()
        cfg = self.host.prepare_run([] if reuse else engines)
        if cfg is None:
            return
        langs = self.languages.codes()
        registry = self.host.registry
        glossaries = tasks.default_glossaries()

        def work(progress: Any, cancel: Any) -> tasks.BenchmarkOutput:
            return tasks.run_benchmark(audio, ref, engines, langs, glossaries, cfg, registry, out, reuse,
                                       lang_audio, lang_ref, ["zh", "en", "ja", "ko"], progress, cancel)

        self.panel.start("引擎評估", work, self._done)

    def _done(self, out: tasks.BenchmarkOutput) -> None:
        for name, err in out.errors.items():
            self.panel.append(f"引擎 {name} 失敗：{err}")
        if out.report_path is None:
            QMessageBox.warning(self, "引擎評估", "沒有任何引擎完成評估，請查看執行紀錄。")
            return
        self.report_path = out.report_path
        self.open_report.setEnabled(True)
        self.report.setMarkdown(out.markdown)
        self.panel.append(f"報告：{out.report_path}")


class GlossaryPage(QWidget):
    """「詞庫」分頁：檢視、開啟、新增、匯入、刪除詞庫 CSV（SPEC 第 8 章）。

    程式內建的預設詞庫（例：道安業務）不可刪除；刪除自訂詞庫時移到資源回收筒，可以還原。
    """

    def __init__(self, on_changed: Any = None) -> None:
        super().__init__()
        self._on_changed = on_changed
        self.protected = default_glossary_names()
        lay = QVBoxLayout(self)
        lay.addWidget(hint("詞庫用來提示語音辨識引擎、校正常見誤寫，並統一翻譯用字。"
                           "按「用 Excel 開啟」編輯，存檔時請保留「CSV UTF-8」格式。"
                           "欄位：" + "、".join(COLUMNS) + "；常見誤寫以「｜」分隔。"))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["詞庫", "類型", "詞數", "最後修改"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.doubleClicked.connect(lambda _i: self.open_selected())
        lay.addWidget(self.table, 1)

        row = QHBoxLayout()
        buttons = [("用 Excel 開啟", self.open_selected), ("新增詞庫", self.new_glossary),
                   ("匯入…", self.import_glossary), ("開啟資料夾", lambda: open_path(user_glossary_dir())),
                   ("重新整理", self.reload)]
        for text, fn in buttons:
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        self.delete_button = QPushButton("刪除")
        self.delete_button.setStyleSheet("QPushButton:enabled { color: #c62828; }")
        self.delete_button.clicked.connect(self.delete_selected)
        row.addWidget(self.delete_button)
        lay.addLayout(row)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.location = hint("")
        lay.addWidget(self.location)
        self.reload()

    def _paths(self) -> list[Path]:
        return tasks.default_glossaries()

    def reload(self) -> None:
        """重新讀取詞庫資料夾。"""
        paths = self._paths()
        self.table.setRowCount(len(paths))
        for r, path in enumerate(paths):
            try:
                count = str(len(load_glossary(path)))
            except (OSError, UnicodeDecodeError, csv.Error):
                count = "無法讀取"
            mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            kind = "🔒 預設（不可刪除）" if self.is_protected(path) else "自訂"
            for c, text in enumerate((path.stem, kind, count, mtime)):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, str(path))
                self.table.setItem(r, c, item)
        if paths and self.table.currentRow() < 0:
            self.table.selectRow(0)
        self.location.setText(f"詞庫位置：{user_glossary_dir()}")
        self._update_buttons()
        if self._on_changed:
            self._on_changed()

    def selected(self) -> Path | None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return Path(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def is_protected(self, path: Path) -> bool:
        """是否為程式內建的預設詞庫。"""
        return path.stem in self.protected

    def _update_buttons(self) -> None:
        path = self.selected()
        protected = path is not None and self.is_protected(path)
        self.delete_button.setEnabled(path is not None and not protected)
        self.delete_button.setToolTip(
            f"「{path.stem}」是預設詞庫，不可刪除。" if protected and path
            else "把選取的詞庫移到資源回收筒（可以還原）。"
        )

    def delete_selected(self) -> None:
        """刪除選取的自訂詞庫（確認後移到資源回收筒）；預設詞庫不可刪除。"""
        from meeting_scribe.storage.trash import move_to_recycle_bin

        path = self.selected()
        if path is None:
            return
        if self.is_protected(path):
            QMessageBox.information(self, "刪除詞庫", f"「{path.stem}」是預設詞庫，不可刪除。")
            return
        try:
            count = len(load_glossary(path))
        except (OSError, UnicodeDecodeError, csv.Error):
            count = 0
        answer = QMessageBox.question(
            self, "刪除詞庫",
            f"確定要刪除詞庫「{path.stem}」（{count} 個詞）嗎？\n\n檔案會移到資源回收筒，需要時可以從回收筒還原。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            move_to_recycle_bin(path)
        except OSError as exc:
            QMessageBox.warning(self, "刪除詞庫", f"刪除失敗：{exc}")
            return
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.reload()

    def open_selected(self) -> None:
        path = self.selected()
        if path:
            open_path(path)

    def new_glossary(self) -> None:
        """建立只有欄位名稱的新詞庫。"""
        name, ok = QInputDialog.getText(self, "新增詞庫", "詞庫名稱（例：共用、道安業務）：")
        name = name.strip()
        if not ok or not name:
            return
        if any(ch in name for ch in '\\/:*?"<>|'):
            QMessageBox.warning(self, "新增詞庫", '名稱不能包含 \\ / : * ? " < > |')
            return
        path = user_glossary_dir() / f"{name}.csv"
        if path.exists():
            QMessageBox.warning(self, "新增詞庫", "已經有同名的詞庫。")
            return
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerow(COLUMNS)
        self.reload()

    def import_glossary(self) -> None:
        """把外部 CSV 複製到詞庫資料夾（先確認有「正確詞」欄位）。"""
        chosen, _ = QFileDialog.getOpenFileName(self, "匯入詞庫", str(Path.home()), CSV_FILTER)
        if not chosen:
            return
        src = Path(chosen)
        try:
            with open(src, encoding="utf-8-sig", newline="") as f:
                header = next(csv.reader(f), [])
        except (OSError, UnicodeDecodeError) as exc:
            QMessageBox.warning(self, "匯入詞庫", f"無法讀取（請確認是 UTF-8 編碼的 CSV）：{exc}")
            return
        if "正確詞" not in header:
            QMessageBox.warning(self, "匯入詞庫", "第一列必須包含「正確詞」欄位。")
            return
        dst = user_glossary_dir() / src.name
        if self.is_protected(dst):
            QMessageBox.warning(self, "匯入詞庫", f"「{dst.stem}」是預設詞庫的名稱，請先把檔案改名再匯入。")
            return
        if dst.exists():
            answer = QMessageBox.question(self, "匯入詞庫", f"已經有「{dst.stem}」，要覆蓋嗎？")
            if answer != QMessageBox.StandardButton.Yes:
                return
        shutil.copyfile(src, dst)
        self.reload()


__all__ = ["BenchmarkPage", "GlossaryPage", "Host", "TranscribePage"]
