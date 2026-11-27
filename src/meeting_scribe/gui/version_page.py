"""「版本」分頁：從 GitHub Releases 更新到新版，或退回 0.5.4 以後的任一版本。"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Protocol

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from meeting_scribe import updater
from meeting_scribe.gui.widgets import OK_COLOR, RunPanel, hint, open_path, primary_button


class VersionHost(Protocol):
    """主視窗提供給本分頁的功能。"""

    cfg: dict[str, Any]

    def busy(self) -> bool: ...
    def statusBar(self) -> Any: ...
    def save_settings(self, quiet: bool = False) -> bool: ...


class _Signals(QObject):
    checked = Signal(object)  # 背景檢查的結果（list[ReleaseInfo] 或 Exception）


class VersionPage(QWidget):
    """「版本」分頁。"""

    def __init__(self, host: Any) -> None:
        super().__init__()
        self.host = host
        self.releases: list[updater.ReleaseInfo] = []
        self._signals = _Signals()
        self._signals.checked.connect(self._background_checked)
        lay = QVBoxLayout(self)

        current = updater.current_version()
        edition = "安裝版" if updater.is_installed() else "開發版（請用 git 更新，這裡只能查看）"
        self.current_label = QLabel(f"<b>目前版本：{current}</b>（{edition}）")
        lay.addWidget(self.current_label)
        lay.addWidget(hint("從 GitHub 官方 Releases 更新或切換版本，只列出 0.5.4 以後的版本（更早的版本沒有這個功能）。"
                           "下載後會比對檢查碼，一致才安裝；設定、API 金鑰與會議資料都會保留。"
                           "只向 GitHub 查詢版本清單，不會送出任何個人或會議資料。"))

        opts = QHBoxLayout()
        settings = host.cfg.get("update", {})
        self.include_pre = QCheckBox("顯示預覽版")
        self.include_pre.setChecked(bool(settings.get("include_prerelease", False)))
        self.include_pre.toggled.connect(lambda _on: (self._save_option("include_prerelease", _on), self.refresh()))
        self.auto_check = QCheckBox("開啟程式時檢查新版本（一天一次，只提醒、不會自動安裝）")
        self.auto_check.setChecked(bool(settings.get("check_on_startup", True)))
        self.auto_check.toggled.connect(lambda on: self._save_option("check_on_startup", on))
        opts.addWidget(self.include_pre)
        opts.addWidget(self.auto_check)
        opts.addStretch(1)
        self.refresh_button = QPushButton("檢查更新")
        self.refresh_button.clicked.connect(self.refresh)
        web = QPushButton("開啟 Releases 網頁")
        web.setToolTip("連不上 GitHub 時，可以從網頁手動下載安裝檔")
        web.clicked.connect(lambda: open_path(updater.releases_page(self.host.cfg)))
        opts.addWidget(self.refresh_button)
        opts.addWidget(web)
        lay.addLayout(opts)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["版本", "類型", "發佈日期", "狀態"])
        header = self.table.horizontalHeader()
        for col in range(3):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.notes = QTextBrowser()
        self.notes.setOpenExternalLinks(True)
        self.notes.setPlaceholderText("選擇一個版本，這裡會顯示更新內容。")
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.notes)
        split.setSizes([180, 260])
        lay.addWidget(split, 2)

        row = QHBoxLayout()
        self.status = hint("按「檢查更新」查詢可以安裝的版本。")
        self.install_button = primary_button("安裝此版本")
        self.install_button.setEnabled(False)
        self.install_button.clicked.connect(self.install_selected)
        row.addWidget(self.status, 1)
        row.addWidget(self.install_button)
        lay.addLayout(row)
        debug_row = QHBoxLayout()
        from meeting_scribe import diagnostics

        self.debug = QCheckBox("除錯模式")
        self.debug.setChecked(diagnostics.debug_enabled(host.cfg))
        self.debug.setToolTip("開啟後，日誌會記錄詳細的處理過程（每次 AI 呼叫、轉錄請求、暫存沿用、版本查詢的時間與狀態），"
                              "仍不含逐字稿內容。\n遇到問題時開啟、重做一次，再按底部的「回報問題」。")
        self.debug.toggled.connect(self._toggle_debug)
        debug_row.addWidget(self.debug)
        debug_row.addWidget(hint("遇到問題時開啟，日誌會記錄更詳細的處理過程（不含逐字稿內容），再按底部的「回報問題」寄給開發者。"), 1)
        lay.addLayout(debug_row)
        self.panel = RunPanel()
        lay.addWidget(self.panel, 1)
        self.panel.busy_changed.connect(lambda busy: self.refresh_button.setEnabled(not busy))

    # ---- 設定 ----
    def _toggle_debug(self, on: bool) -> None:
        from meeting_scribe import diagnostics
        from meeting_scribe.config import load_config, save_user_overrides

        diagnostics.apply_debug(on)
        try:
            save_user_overrides({"general": {"debug": on}})
            self.host.cfg = load_config()
        except OSError:
            pass
        self.host.statusBar().showMessage(f"除錯模式已{'開啟' if on else '關閉'}", 5000)

    def _save_option(self, key: str, value: Any) -> None:
        from meeting_scribe.config import load_config, save_user_overrides

        try:
            save_user_overrides({"update": {key: value}})
            self.host.cfg = load_config()
        except OSError:
            pass

    # ---- 查詢 ----
    def refresh(self) -> None:
        """在背景查詢 GitHub 上的版本。"""
        if self.panel.running:
            return
        cfg, include = self.host.cfg, self.include_pre.isChecked()

        def work(progress: Any, _cancel: Any) -> list[updater.ReleaseInfo]:
            progress(0, "向 GitHub 查詢版本清單…")
            return updater.list_releases(cfg, include)

        self.panel.start("檢查更新", work, self.show_releases)

    def show_releases(self, releases: list[updater.ReleaseInfo]) -> None:
        self.releases = releases
        current = updater.parse_version(updater.current_version())
        self.table.setRowCount(len(releases))
        for row, rel in enumerate(releases):
            if rel.key == current:
                state = "目前版本"
            elif rel.key > current:
                state = "較新，可以更新"
            else:
                state = "較舊，可以退回"
            items = [rel.version, "預覽版" if rel.prerelease else "穩定版",
                     rel.published.astimezone().strftime("%Y-%m-%d") if rel.published else "—", state]
            for col, text in enumerate(items):
                item = QTableWidgetItem(text)
                if rel.key == current:
                    item.setForeground(Qt.GlobalColor.darkGreen)
                self.table.setItem(row, col, item)
        newer = updater.newer_than_current(releases, self.include_pre.isChecked())
        if not releases:
            self.status.setText("沒有可以安裝的版本。")
        elif newer:
            self.status.setText(f"<span style='color:{OK_COLOR}'><b>有新版本：{newer.version}</b></span>"
                                "　選擇後按「更新到此版本」。")
        else:
            self.status.setText("目前已是最新版本。")
        if releases:
            self.table.selectRow(0)
        self._mark_tab(newer is not None)

    def _selection_changed(self) -> None:
        rel = self.selected()
        if rel is None:
            self.install_button.setEnabled(False)
            self.notes.clear()
            return
        self.notes.setMarkdown(f"# {rel.title}\n\n{rel.notes}")
        current = updater.parse_version(updater.current_version())
        if rel.key == current:
            self.install_button.setText("目前版本")
            self.install_button.setEnabled(False)
            return
        self.install_button.setText(f"{'更新' if rel.key > current else '退回'}到 {rel.version}")
        self.install_button.setEnabled(updater.is_installed())
        self.install_button.setToolTip("" if updater.is_installed() else "開發版請用 git 切換版本")

    def selected(self) -> updater.ReleaseInfo | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self.releases[rows[0].row()] if rows else None

    # ---- 安裝 ----
    def install_selected(self) -> None:
        rel = self.selected()
        if rel is None or self.panel.running:
            return
        if not updater.is_installed():
            QMessageBox.information(self, "版本", "開發版請用 git 切換版本。")
            return
        if self.host.busy() or self.host.document.panel.running:
            QMessageBox.information(self, "版本", "還有工作在執行，請等它完成或取消後再更新。")
            return
        if not self.host.document._confirm_discard():  # 逐字稿有未儲存的修改
            return
        newer = rel.key > updater.parse_version(updater.current_version())
        action = "更新" if newer else "退回"
        since = updater.parse_version(str(self.host.cfg.get("update", {}).get("self_update_since", "0.5.6")))
        if newer:
            extra = ""
        elif rel.key < since:
            extra = (f"\n\n⚠ {rel.version} 還不能在程式裡更新：退回後，"
                     "之後要更新請到 GitHub Releases 網頁手動下載新版安裝檔。")
        else:
            extra = ("\n\n退回舊版後，新版本才有的功能就不能使用；"
                     "用新版存的文件仍可開啟（新版多出的資料會被略過）。之後可以再到「版本」分頁更新。")
        answer = QMessageBox.question(
            self, f"{action}到 {rel.version}",
            f"要{action}到 {rel.version} 嗎？\n\n"
            f"・會下載約 {rel.installer_size / 1048576:.0f} MB 的安裝檔並比對檢查碼\n"
            "・下載完成後程式會關閉並自動安裝，安裝完成後自動重新開啟\n"
            f"・設定、API 金鑰與會議資料都會保留{extra}")
        if answer != QMessageBox.StandardButton.Yes:
            return

        def work(progress: Any, cancel: Any) -> Path:
            return updater.download(rel, progress, cancel)

        self.panel.start(f"下載 {rel.version}", work, lambda path: self._ready_to_install(rel, path))

    def _ready_to_install(self, rel: updater.ReleaseInfo, path: Path) -> None:
        self.panel.append(f"已下載並驗證檢查碼：{path.name}")
        self.host.save_settings(quiet=True)
        updater.launch_installer(path)
        self.panel.append("開始安裝，程式即將關閉…")
        app = QApplication.instance()
        if app is not None:
            app.quit()

    # ---- 開啟程式時檢查 ----
    def check_in_background(self) -> None:
        """一天一次在背景檢查新版本；有新版時只在狀態列與分頁名稱提醒。"""
        import os

        if os.environ.get("MEETINGSCRIBE_NO_UPDATE_CHECK"):  # 測試或離線環境不連網
            return
        if not updater.is_installed() or not updater.should_check_today(self.host.cfg):
            return
        cfg, include = self.host.cfg, self.include_pre.isChecked()
        signals = self._signals  # 執行緒自己持有訊號物件：視窗關閉後仍可安全結束

        def run() -> None:
            try:
                result: Any = updater.list_releases(cfg, include)
            except Exception as exc:  # noqa: BLE001 - 背景檢查失敗不打擾使用者
                result = exc
            try:
                signals.checked.emit(result)
            except RuntimeError:  # 視窗已經關閉：不需要提醒
                pass

        threading.Thread(target=run, daemon=True, name="檢查更新").start()

    def _background_checked(self, result: Any) -> None:
        from datetime import datetime

        self._save_option("last_check", datetime.now().strftime("%Y-%m-%d"))
        if isinstance(result, Exception):
            return
        self.show_releases(result)
        newer = updater.newer_than_current(result, self.include_pre.isChecked())
        if newer:
            self.host.statusBar().showMessage(f"有新版本 {newer.version}，可以到「版本」分頁更新。", 15000)

    def _mark_tab(self, has_new: bool) -> None:
        tabs = getattr(self.host, "tabs", None)
        if tabs is None:
            return
        idx = tabs.indexOf(self)
        if idx >= 0:
            tabs.setTabText(idx, "版本（有新版）" if has_new else "版本")
