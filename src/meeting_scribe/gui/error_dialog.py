"""圖形介面的錯誤安全網：任何沒有被處理的錯誤都記錄到日誌，並顯示中文說明視窗，程式可以繼續使用。

- 背景執行緒發生的錯誤透過 Qt 訊號轉到主執行緒顯示（不可在背景執行緒直接開視窗）。
- 連續發生多個錯誤時只顯示一個視窗，其餘只記錄，避免畫面被視窗淹沒。
- 視窗按鈕：回報問題（產生回報檔）、開啟日誌、複製錯誤資訊、關閉。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from meeting_scribe import diagnostics
from meeting_scribe.gui.widgets import open_path


def show_report_created(parent: QWidget | None, note: str = "") -> None:
    """產生問題回報檔並告訴使用者位置（開啟資料夾並選取檔案）。"""
    import subprocess

    try:
        path = diagnostics.make_report(note=note)
    except Exception as exc:  # noqa: BLE001 - 產生失敗也要告訴使用者
        diagnostics.log_exception(exc, "產生問題回報檔")
        QMessageBox.warning(parent, "回報問題", f"無法產生回報檔：{exc}")
        return
    try:
        subprocess.Popen(["explorer", f"/select,{path}"])
    except OSError:
        open_path(path.parent)
    QMessageBox.information(
        parent, "回報問題",
        f"已產生問題回報檔：\n{path}\n\n"
        "內容只有程式版本、系統資訊、日誌與設定，不含錄音、逐字稿與 API 金鑰。\n"
        "請把這個檔案寄給開發者，並說明當時正在做什麼。")


class ErrorReporter(QObject):
    """把錯誤轉到主執行緒顯示說明視窗。"""

    raised = Signal(str, str)  # (給使用者的說明, 完整錯誤細節)

    def __init__(self) -> None:
        super().__init__()
        self._showing = False
        self.suppressed = 0
        self.shown: list[str] = []  # 測試用：顯示過的說明
        self.raised.connect(self._show)

    def report(self, exc: BaseException, where: str = "") -> None:
        """可以在任何執行緒呼叫。"""
        message = diagnostics.describe(exc)
        if where:
            message = f"{where}：{message}"
        self.raised.emit(message, diagnostics.format_error(exc))

    def _show(self, message: str, detail: str) -> None:
        if self._showing:
            self.suppressed += 1
            return
        self._showing = True
        self.shown.append(message)
        try:
            parent = QApplication.activeWindow()
            box = QMessageBox(parent)
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle("程式發生錯誤")
            box.setText(message)
            box.setInformativeText("您目前的工作沒有遺失：已完成的轉錄與 AI 結果都有存檔，程式可以繼續使用。")
            box.setDetailedText(detail)
            report = box.addButton("回報問題", QMessageBox.ButtonRole.ActionRole)
            logs = box.addButton("開啟日誌", QMessageBox.ButtonRole.ActionRole)
            copy = box.addButton("複製錯誤資訊", QMessageBox.ButtonRole.ActionRole)
            box.addButton("關閉", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is report:
                show_report_created(parent, message + "\n\n" + detail)
            elif clicked is logs:
                folder = diagnostics.log_dir()
                folder.mkdir(parents=True, exist_ok=True)
                open_path(folder)
            elif clicked is copy:
                info = "\n".join(diagnostics.system_info())
                QApplication.clipboard().setText(f"{message}\n\n{info}\n\n{detail}")
        finally:
            self._showing = False


_reporter: ErrorReporter | None = None


def install() -> ErrorReporter:
    """安裝全域錯誤攔截（需先建立 QApplication）。"""
    global _reporter
    if _reporter is None:
        _reporter = ErrorReporter()
    reporter = _reporter

    def on_error(exc: BaseException, where: str) -> None:
        reporter.report(exc, where)

    diagnostics.install_excepthooks(on_error)
    return reporter


def reporter() -> ErrorReporter | None:
    return _reporter

