"""主視窗：所有功能整合在同一個視窗的分頁中。

分頁：轉錄｜引擎評估｜詞庫｜API 金鑰
- 引擎、會議語言、輸出資料夾在「轉錄」分頁選擇，程式會記住上次的選擇（不另設預設值分頁）。
- 工作分頁的主要按鈕（藍色）在分頁下方，執行時顯示進度、經過時間與執行紀錄，可以取消。
- 按下執行時會先自動儲存設定、檢查金鑰與 ffmpeg；缺少時直接切到對應分頁並說明。
- 底部工具列：開啟設定檔（進階）、開啟日誌、建立桌面捷徑、儲存設定、結束。

完整的「新工作／進度／審閱」流程於第 4 階段加入。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtGui import QCloseEvent, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from meeting_scribe import config as app_config
from meeting_scribe import tasks
from meeting_scribe.gui.document_page import DocumentPage
from meeting_scribe.gui.icon import make_pixmap
from meeting_scribe.gui.pages import BenchmarkPage, GlossaryPage, TranscribePage
from meeting_scribe.gui.settings import KeysPage
from meeting_scribe.gui.widgets import APP_STYLE, open_path, primary_button
from meeting_scribe.postprocess.lang_detect import LanguageRegistry

STAGE_TEXT = "目前進度：第 0 階段（引擎評估）"


class MainWindow(QMainWindow):
    """MeetingScribe 主視窗。"""

    def __init__(self, secret_store: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.registry = LanguageRegistry.load()
        self.cfg = app_config.load_config()
        self.setWindowTitle("MeetingScribe 會議紀錄產生器")
        self.resize(900, 780)

        header = QWidget()
        h = QHBoxLayout(header)
        logo = QLabel()
        logo.setPixmap(make_pixmap(40))
        title = QLabel("<b style='font-size:15pt'>MeetingScribe 會議紀錄產生器</b>")
        h.addWidget(logo)
        h.addWidget(title)
        h.addStretch(1)
        stage = QLabel(STAGE_TEXT)
        stage.setStyleSheet("color: gray;")
        h.addWidget(stage)

        out = app_config.output_dir(self.cfg)
        self.transcribe = TranscribePage(self, self.cfg, out)
        self.benchmark = BenchmarkPage(self, self.cfg, out)
        self.document = DocumentPage(self)
        self.glossary = GlossaryPage(on_changed=self._glossaries_changed)
        self.keys = KeysPage(lambda: self.cfg, **(secret_store or {}))
        self.keys.keys_changed.connect(self.engines_changed)
        self.keys.llm_changed.connect(lambda _c: self.engines_changed())
        self.keys.stt_changed.connect(self._stt_from_keys)

        self.tabs = QTabWidget()
        self._pages = {
            "transcribe": (self.transcribe, "轉錄"),
            "document": (self.document, "產出文件"),
            "benchmark": (self.benchmark, "引擎評估"),
            "glossary": (self.glossary, "詞庫"),
            "keys": (self.keys, "API 金鑰"),
        }
        for page, label in self._pages.values():
            self.tabs.addTab(page, label)
        self.tabs.currentChanged.connect(
            lambda _i: self.tabs.currentWidget() is self.keys and self.keys.refresh_usage())
        self.tabs.setTabToolTip(0, "把錄音檔轉成逐字稿")
        self.tabs.setTabToolTip(1, "修正逐字稿、填寫會議資訊、產出文件（也可以匯入已經有的逐字稿）")
        self.tabs.setTabToolTip(2, "第 0 階段：比較語音轉文字引擎，決定預設引擎")

        bottom = QHBoxLayout()
        tools = [
            ("開啟設定檔（進階）", "用記事本開啟 config.toml，可調整視窗裡沒有的細節。", self._open_config),
            ("開啟日誌", "日誌只記錄階段、時間與錯誤，不含逐字稿內容。",
             lambda: open_path(self._ensure(app_config.localappdata_dir() / "logs"))),
            ("建立桌面捷徑", "在桌面建立「MeetingScribe 會議紀錄」捷徑（已有的話會覆蓋）。", self._make_shortcut),
        ]
        for text, tip, fn in tools:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            bottom.addWidget(b)
        bottom.addStretch(1)
        self.save_button = primary_button("儲存設定")
        self.save_button.setToolTip("儲存金鑰、區域與模型設定，以及「轉錄」分頁的選擇（Ctrl+S）")
        self.save_button.clicked.connect(lambda: self.save_settings())
        quit_button = QPushButton("結束")
        quit_button.clicked.connect(self.close)
        bottom.addWidget(self.save_button)
        bottom.addWidget(quit_button)
        QShortcut(QKeySequence.StandardKey.Save, self, activated=lambda: self.save_settings())

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.addWidget(header)
        lay.addWidget(self.tabs, 1)
        lay.addLayout(bottom)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

        for page in (self.transcribe, self.benchmark):
            page.panel.busy_changed.connect(self._set_busy)
        self.engines_changed()
        self._first_tab()

    # ---- 給分頁使用的功能 ----
    def go_to(self, page: str) -> None:
        """切換分頁。"""
        self.tabs.setCurrentWidget(self._pages[page][0])

    def engines_changed(self) -> None:
        """「轉錄」分頁換引擎時，同步「API 金鑰」的語音轉錄服務，並更新必要／建議金鑰提示。"""
        from meeting_scribe.llm import LLM_PROVIDERS

        engine = self.transcribe.selected_engines()[0]
        self.keys.set_stt(engine)
        recommended = {}
        if engine == "soniox":
            recommended[LLM_PROVIDERS[self.keys.llm_provider()][1]] = "Soniox 的 AI 臺灣在地化會用到"
        self.keys.set_needed({app_config.ENGINE_KEYS[engine]}, recommended)

    def open_transcription(self, out: tasks.TranscribeOutput, audio: Path | None) -> None:
        """轉錄完成：把結果帶到「產出文件」分頁並切換過去。"""
        from meeting_scribe.transcript_io import from_stt_result

        if self.document.dirty and not self.document._confirm_discard():
            self.statusBar().showMessage("轉錄已完成；可在「產出文件」分頁用「匯入逐字稿檔案…」開啟 stt_raw.json", 10000)
            return
        self.document.load(from_stt_result(out.result, out.folder, audio))
        self.go_to("document")
        self.statusBar().showMessage("轉錄完成：請修正逐字稿、填寫會議資訊後產出文件。", 10000)

    def remember_meeting_fields(self, location: str, chair: str, recorder: str) -> None:
        """記住最近 10 個會議地點、主席，以及上次的紀錄人員。"""
        ui = self.cfg.get("ui", {})

        def push(items: list[str], value: str) -> list[str]:
            return ([value] if value else []) + [i for i in items if i and i != value][:9]

        updates = {"ui": {
            "recent_locations": push(list(ui.get("recent_locations", [])), location),
            "recent_chairs": push(list(ui.get("recent_chairs", [])), chair),
            "last_recorder": recorder or ui.get("last_recorder", ""),
        }}
        try:
            app_config.save_user_overrides(updates)
            self.cfg = app_config.load_config()
        except OSError:
            pass

    def _stt_from_keys(self, code: str) -> None:
        """在「API 金鑰」換語音轉錄服務時，同步「轉錄」分頁的引擎。"""
        combo = self.transcribe.engine
        idx = combo.findData(code)
        if idx >= 0 and idx != combo.currentIndex():
            combo.setCurrentIndex(idx)  # 會觸發 engines_changed
        else:
            self.engines_changed()

    def prepare_run(self, engines: list[str]) -> dict[str, Any] | None:
        """執行前：儲存設定 → 檢查金鑰、Azure 區域、ffmpeg。可以執行時回傳最新設定。"""
        if self.busy():
            QMessageBox.information(self, "請稍候", "另一個工作正在執行，請等它完成或先取消。")
            return None
        if not self.save_settings(quiet=True):
            return None
        from meeting_scribe import usage

        budget = usage.budget_status(self.cfg)
        if engines and budget.exceeded:
            self.go_to("keys")
            self.keys.refresh_usage()
            self.keys.budget_limit.setFocus()
            QMessageBox.warning(self, "已達費用上限",
                                f"本月估計費用 US$ {budget.used:.2f} 已達上限 US$ {budget.limit:.2f}，"
                                "已停止新的工作。\n\n如需繼續，請在「API 金鑰」分頁的「3. 費用」提高上限後按「儲存設定」。")
            return None
        if engines and budget.warning:
            self.statusBar().showMessage(
                f"提醒：本月估計費用 US$ {budget.used:.2f}，已用掉上限的 {budget.ratio:.0%}。", 15000)
        missing = tasks.missing_keys(engines)
        if missing:
            self.go_to("keys")
            first = next(e for e in engines if app_config.ENGINE_KEYS.get(e) in missing)
            self.keys.set_stt(first)
            names = "\n".join(f"・{app_config.SECRET_NAMES[k]}" for k in missing)
            QMessageBox.warning(self, "缺少 API 金鑰",
                                f"所選引擎需要以下金鑰，但尚未設定：\n\n{names}\n\n請貼上金鑰並按「儲存」。")
            return None
        az = self.cfg.get("stt", {}).get("azure", {})
        if "azure" in engines and not (az.get("region") or az.get("endpoint")):
            self.go_to("keys")
            self.keys.set_stt("azure")
            self.keys.azure_region.setFocus()
            QMessageBox.warning(self, "缺少 Azure 區域", "請在「API 金鑰」分頁的語音轉錄區塊選擇 Azure 區域。")
            return None
        if engines:
            from meeting_scribe.audio.preprocess import AudioError, find_ffmpeg

            try:
                find_ffmpeg(self.cfg)
            except AudioError as exc:
                QMessageBox.warning(self, "找不到 ffmpeg", str(exc))
                return None
        return self.cfg

    # ---- 設定 ----
    def save_settings(self, quiet: bool = False) -> bool:
        """儲存已輸入的金鑰與設定；quiet=True 時只在狀態列提示。"""
        saved_keys = self.keys.save_pending()
        try:
            app_config.save_user_overrides(self._settings_updates())
            self.cfg = app_config.load_config()
        except Exception as exc:  # noqa: BLE001 - 顯示給使用者
            QMessageBox.critical(self, "儲存失敗", f"無法儲存設定：{exc}")
            return False
        msg = "設定已儲存" + (f"；已更新 {len(saved_keys)} 把金鑰" if saved_keys else "")
        self.statusBar().showMessage(msg, 5000)
        if not quiet:
            QMessageBox.information(self, "儲存設定", msg + "。")
        return True

    def _settings_updates(self) -> dict[str, Any]:
        """要寫入設定檔的內容：各服務的區域與模型，以及「轉錄」分頁的選擇（下次開啟時沿用）。"""
        out = self.transcribe.output.path()
        default_out = app_config.output_dir({})
        updates = self.keys.updates()
        updates["stt"]["default_engine"] = self.transcribe.selected_engines()[0]
        updates["general"] = {"output_dir": "" if out is None or out == default_out else str(out)}
        updates["ui"] = {"last_languages": self.transcribe.languages.codes()}
        return updates

    def _open_config(self) -> None:
        path = app_config.init_user_config()
        open_path(path)
        self.statusBar().showMessage("修改設定檔後，請重新開啟程式套用。", 8000)

    def _make_shortcut(self) -> None:
        from meeting_scribe.gui.shortcut import create_desktop_shortcut

        try:
            link = create_desktop_shortcut()
        except (OSError, RuntimeError) as exc:
            QMessageBox.warning(self, "建立桌面捷徑", f"建立失敗：{exc}")
            return
        QMessageBox.information(self, "建立桌面捷徑", f"已在桌面建立捷徑：\n{link}")

    @staticmethod
    def _ensure(path: Any) -> Any:
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _glossaries_changed(self) -> None:
        if hasattr(self, "transcribe"):
            self.transcribe.glossaries.reload()

    def _first_tab(self) -> None:
        """第一次使用（預設引擎沒有金鑰）時先開「API 金鑰」分頁。"""
        if tasks.missing_keys([self.cfg.get("stt", {}).get("default_engine", "azure")]):
            self.go_to("keys")
            self.statusBar().showMessage("第一次使用：請先貼上 API 金鑰並按「儲存」。", 15000)

    # ---- 執行狀態 ----
    def busy(self) -> bool:
        return self.transcribe.panel.running or self.benchmark.panel.running

    def _set_busy(self, _busy: bool) -> None:
        busy = self.busy()
        if not busy:
            self.keys.refresh_usage()  # 工作結束後更新已使用費用
        for b in (self.transcribe.run_button, self.benchmark.run_button, self.benchmark.draft_button):
            b.setEnabled(not busy)

    def closeEvent(self, event: QCloseEvent) -> None:
        """關閉前：提醒執行中的工作與尚未儲存的金鑰。"""
        if self.busy():
            answer = QMessageBox.question(self, "結束", "工作還在執行，確定要結束嗎？（已完成的部分會保留）")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.transcribe.panel.cancel()
            self.benchmark.panel.cancel()
        if self.keys.pending():
            answer = QMessageBox.question(
                self, "尚未儲存", "有輸入的金鑰尚未儲存，要先儲存嗎？",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if answer == QMessageBox.StandardButton.Save:
                self.keys.save_pending()
        if not self.document._confirm_discard():  # 逐字稿有未儲存的修改
            event.ignore()
            return
        try:
            app_config.save_user_overrides(self._settings_updates())  # 記住這次的選擇
        except Exception:  # noqa: BLE001, S110 - 關閉時無法存檔不影響結束
            pass
        event.accept()


def run_gui() -> int:
    """啟動圖形介面。"""
    from meeting_scribe.cli import setup_logging
    from meeting_scribe.gui.icon import make_icon

    setup_logging(console=False)
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("MeetingScribe")
    app.setFont(QFont("Microsoft JhengHei UI", 10))
    app.setStyleSheet(APP_STYLE)
    app.setWindowIcon(make_icon())
    win = MainWindow()
    win.show()
    return int(app.exec())
