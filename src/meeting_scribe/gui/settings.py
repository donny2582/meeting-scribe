"""設定視窗（SPEC 4.4 的金鑰與引擎部分）。

- API 金鑰存在 Windows 認證管理員，畫面只顯示末 4 碼。
- 引擎區域、模型名稱、單價寫入使用者設定檔 ``%APPDATA%\\MeetingScribe\\config.toml``。
- 「測試連線」在背景執行緒進行，介面不會卡住。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from meeting_scribe import config as app_config

AZURE_REGIONS = ["eastasia", "southeastasia", "japaneast", "japanwest", "koreacentral",
                 "eastus", "westus2", "westeurope"]


@dataclass
class KeyRow:
    """一列金鑰欄位的元件。"""

    name: str
    status: QLabel
    edit: QLineEdit
    show: QCheckBox
    test: QPushButton
    clear: QPushButton


def check_key(name: str, key: str, cfg: dict[str, Any]) -> tuple[bool, str]:
    """以最小的唯讀請求確認金鑰是否有效，回傳（是否成功, 說明）。"""
    stt = cfg.get("stt", {})
    try:
        if name == "openai_api_key":
            base = stt.get("openai", {}).get("base_url", "https://api.openai.com/v1").rstrip("/")
            resp = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=20)
        elif name == "anthropic_api_key":
            resp = httpx.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                timeout=20,
            )
        elif name == "azure_speech_key":
            az = stt.get("azure", {})
            endpoint = (az.get("endpoint") or "").rstrip("/")
            if not endpoint:
                if not az.get("region"):
                    return False, "請先選擇 Azure 區域並儲存"
                endpoint = f"https://{az['region']}.api.cognitive.microsoft.com"
            resp = httpx.get(
                f"{endpoint}/speechtotext/models/base",
                params={"api-version": az.get("api_version", "2024-11-15")},
                headers={"Ocp-Apim-Subscription-Key": key},
                timeout=20,
            )
        else:
            return False, "未知的金鑰"
    except httpx.HTTPError as exc:
        return False, f"無法連線：{type(exc).__name__}"
    if resp.status_code == 200:
        return True, "連線成功，金鑰有效"
    if resp.status_code in (401, 403):
        return False, f"金鑰無效或沒有權限（HTTP {resp.status_code}）"
    if resp.status_code == 429:
        return False, "請求過多或額度不足（HTTP 429）"
    return False, f"伺服器回應 HTTP {resp.status_code}，請確認區域或端點設定"


class _TestSignals(QObject):
    done = Signal(str, bool, str)


class SettingsWindow(QWidget):
    """設定視窗：API 金鑰、引擎區域、模型名稱、單價。"""

    def __init__(
        self,
        get_secret: Callable[[str], str | None] = app_config.get_secret,
        set_secret: Callable[[str, str], None] = app_config.set_secret,
        delete_secret: Callable[[str], None] = app_config.delete_secret,
    ) -> None:
        super().__init__()
        self._get_secret = get_secret
        self._set_secret = set_secret
        self._delete_secret = delete_secret
        self.cfg = app_config.load_config()
        self.rows: dict[str, KeyRow] = {}
        self._signals = _TestSignals()
        self._signals.done.connect(self._on_test_done)

        self.setWindowTitle("MeetingScribe 設定")
        self.setMinimumWidth(760)
        layout = QVBoxLayout(self)
        layout.addWidget(self._build_keys())
        layout.addWidget(self._build_engines())
        layout.addWidget(self._build_prices())

        hint = QLabel(
            f"金鑰存在 Windows 認證管理員，畫面只顯示末 4 碼。\n其他設定存在：{app_config.user_config_path()}"
        )
        hint.setStyleSheet("color: gray;")
        hint.setWordWrap(True)
        hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.save_button = QPushButton("儲存")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.save)
        close_button = QPushButton("關閉")
        close_button.clicked.connect(self.close)
        buttons.addWidget(self.save_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        self.refresh_status()

    # ---- 版面 ----
    def _build_keys(self) -> QGroupBox:
        box = QGroupBox("API 金鑰")
        grid = QGridLayout(box)
        for col, title in enumerate(["服務", "目前", "輸入新金鑰（留空表示不變更）"]):
            head = QLabel(title)
            head.setStyleSheet("font-weight: bold;")
            grid.addWidget(head, 0, col)
        for r, (name, label) in enumerate(app_config.SECRET_NAMES.items(), start=1):
            status = QLabel()
            edit = QLineEdit()
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.setPlaceholderText("貼上金鑰")
            edit.setMinimumWidth(280)
            show = QCheckBox("顯示")
            show.toggled.connect(
                lambda on, e=edit: e.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password)
            )
            test = QPushButton("測試連線")
            test.clicked.connect(lambda _=False, n=name: self.test_key(n))
            clear = QPushButton("清除")
            clear.clicked.connect(lambda _=False, n=name: self.clear_key(n))
            grid.addWidget(QLabel(label), r, 0)
            grid.addWidget(status, r, 1)
            grid.addWidget(edit, r, 2)
            grid.addWidget(show, r, 3)
            grid.addWidget(test, r, 4)
            grid.addWidget(clear, r, 5)
            self.rows[name] = KeyRow(name, status, edit, show, test, clear)
        return box

    def _build_engines(self) -> QGroupBox:
        box = QGroupBox("語音轉文字引擎")
        form = QFormLayout(box)
        stt = self.cfg.get("stt", {})
        self.azure_region = QComboBox()
        self.azure_region.setEditable(True)
        self.azure_region.addItems(AZURE_REGIONS)
        self.azure_region.setCurrentText(stt.get("azure", {}).get("region", ""))
        self.azure_region.lineEdit().setPlaceholderText("請選擇或輸入 Azure 資源所在區域，例：eastasia")
        self.azure_endpoint = QLineEdit(stt.get("azure", {}).get("endpoint", ""))
        self.azure_endpoint.setPlaceholderText("選填，例：https://<資源名稱>.cognitiveservices.azure.com")
        self.openai_model = QLineEdit(stt.get("openai", {}).get("model", ""))
        self.default_engine = QComboBox()
        self.default_engine.addItem("Azure AI Speech", "azure")
        self.default_engine.addItem("OpenAI", "openai")
        idx = self.default_engine.findData(stt.get("default_engine", "azure"))
        self.default_engine.setCurrentIndex(max(idx, 0))
        form.addRow("Azure 區域：", self.azure_region)
        form.addRow("Azure 自訂端點：", self.azure_endpoint)
        form.addRow("OpenAI 模型名稱：", self.openai_model)
        form.addRow("預設引擎：", self.default_engine)
        return box

    def _build_prices(self) -> QGroupBox:
        box = QGroupBox("API 單價（用來估算費用，請依官方價目表填寫）")
        form = QFormLayout(box)
        stt = self.cfg.get("stt", {})
        self.azure_price = self._price_box(stt.get("azure", {}).get("price_per_hour", 0.0))
        self.openai_price = self._price_box(stt.get("openai", {}).get("price_per_hour", 0.0))
        form.addRow("Azure：", self.azure_price)
        form.addRow("OpenAI：", self.openai_price)
        return box

    @staticmethod
    def _price_box(value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 1000.0)
        spin.setDecimals(3)
        spin.setSingleStep(0.1)
        spin.setSuffix(" 美元／音訊小時")
        spin.setValue(float(value or 0.0))
        return spin

    # ---- 動作 ----
    def refresh_status(self) -> None:
        """更新各金鑰「目前」欄位。"""
        for name, row in self.rows.items():
            row.status.setText(app_config.mask_secret(self._get_secret(name)))

    def _has_unsaved_keys(self) -> bool:
        return any(row.edit.text().strip() for row in self.rows.values())

    def save(self) -> bool:
        """儲存金鑰與設定。"""
        saved: list[str] = []
        try:
            for name, row in self.rows.items():
                value = row.edit.text().strip()
                if value:
                    self._set_secret(name, value)
                    row.edit.clear()
                    saved.append(app_config.SECRET_NAMES[name])
            app_config.save_user_overrides({
                "stt": {
                    "default_engine": self.default_engine.currentData(),
                    "azure": {
                        "region": self.azure_region.currentText().strip(),
                        "endpoint": self.azure_endpoint.text().strip(),
                        "price_per_hour": self.azure_price.value(),
                    },
                    "openai": {
                        "model": self.openai_model.text().strip(),
                        "price_per_hour": self.openai_price.value(),
                    },
                }
            })
        except Exception as exc:  # noqa: BLE001 - 顯示給使用者
            QMessageBox.critical(self, "儲存失敗", f"無法儲存設定：{exc}")
            return False
        self.cfg = app_config.load_config()
        self.refresh_status()
        msg = "設定已儲存。"
        if saved:
            msg += "\n已更新金鑰：" + "、".join(saved)
        QMessageBox.information(self, "已儲存", msg)
        return True

    def clear_key(self, name: str) -> None:
        """刪除已儲存的金鑰（先確認）。"""
        label = app_config.SECRET_NAMES[name]
        answer = QMessageBox.question(self, "清除金鑰", f"確定要從 Windows 認證管理員刪除「{label}」嗎？")
        if answer == QMessageBox.StandardButton.Yes:
            self._delete_secret(name)
            self.refresh_status()

    def test_key(self, name: str) -> None:
        """在背景執行緒測試金鑰；有輸入新金鑰時測試新金鑰。"""
        row = self.rows[name]
        key = row.edit.text().strip() or self._get_secret(name)
        if not key:
            QMessageBox.warning(self, "測試連線", "請先輸入金鑰。")
            return
        cfg = app_config.load_config()
        cfg["stt"]["azure"]["region"] = self.azure_region.currentText().strip()
        cfg["stt"]["azure"]["endpoint"] = self.azure_endpoint.text().strip()
        row.test.setEnabled(False)
        row.test.setText("測試中…")

        def work() -> None:
            ok, msg = check_key(name, key, cfg)
            self._signals.done.emit(name, ok, msg)

        threading.Thread(target=work, daemon=True).start()

    def _on_test_done(self, name: str, ok: bool, msg: str) -> None:
        row = self.rows[name]
        row.test.setEnabled(True)
        row.test.setText("測試連線")
        label = app_config.SECRET_NAMES[name]
        if ok:
            QMessageBox.information(self, "測試連線", f"{label}：{msg}")
        else:
            QMessageBox.warning(self, "測試連線", f"{label}：{msg}")

    def closeEvent(self, event: QCloseEvent) -> None:
        """關閉前提醒尚未儲存的金鑰。"""
        if self._has_unsaved_keys():
            answer = QMessageBox.question(
                self, "尚未儲存", "有輸入的金鑰尚未儲存，要先儲存嗎？",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if answer == QMessageBox.StandardButton.Save and not self.save():
                event.ignore()
                return
        event.accept()


def run_settings() -> int:
    """單獨開啟設定視窗。"""
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("Microsoft JhengHei UI", 10))
    win = SettingsWindow()
    win.show()
    return int(app.exec())
