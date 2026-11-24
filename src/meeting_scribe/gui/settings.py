"""設定分頁（SPEC 4.4）：「API 金鑰」與「服務與費用」。

- API 金鑰存在 Windows 認證管理員，畫面只顯示末 4 碼；狀態以顏色區分：
  綠＝已設定、紅＝目前選的引擎需要但尚未設定、灰＝目前用不到。
- 其他設定寫入使用者設定檔 ``%APPDATA%\\MeetingScribe\\config.toml``（只寫覆寫的項目）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
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
from meeting_scribe.gui.widgets import ERROR_COLOR, OK_COLOR, FilePicker, LanguagePicker, hint, open_path
from meeting_scribe.postprocess.lang_detect import LanguageRegistry

AZURE_REGIONS = ["eastasia", "southeastasia", "japaneast", "japanwest", "koreacentral",
                 "eastus", "westus2", "westeurope"]
ENGINES = (("azure", "Azure AI Speech"), ("openai", "OpenAI"), ("soniox", "Soniox"))
KEY_HELP = {
    "azure_speech_key": "Azure 入口網站 → 語音服務資源 →「金鑰與端點」",
    "openai_api_key": "platform.openai.com → API keys",
    "soniox_api_key": "console.soniox.com → 專案 → API Keys",
    "anthropic_api_key": "console.anthropic.com → API Keys（第 1 階段臺灣在地化、第 2 階段摘要使用）",
}


def check_key(name: str, key: str, cfg: dict[str, Any]) -> tuple[bool, str]:
    """以最小的唯讀請求確認金鑰是否有效，回傳（是否成功, 說明）。"""
    stt = cfg.get("stt", {})
    try:
        if name == "openai_api_key":
            base = stt.get("openai", {}).get("base_url", "https://api.openai.com/v1").rstrip("/")
            resp = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=20)
        elif name == "soniox_api_key":
            base = stt.get("soniox", {}).get("base_url", "https://api.soniox.com/v1").rstrip("/")
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
                    return False, "請先在「服務與費用」分頁選擇 Azure 區域"
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


@dataclass
class KeyRow:
    """一把金鑰的元件。"""

    name: str
    status: QLabel
    edit: QLineEdit
    show: QCheckBox
    save: QPushButton
    test: QPushButton
    clear: QPushButton


class _TestSignals(QObject):
    done = Signal(str, bool, str)


class KeysPage(QWidget):
    """「API 金鑰」分頁。"""

    keys_changed = Signal()

    def __init__(
        self,
        cfg_provider: Callable[[], dict[str, Any]],
        get_secret: Callable[[str], str | None] = app_config.get_secret,
        set_secret: Callable[[str, str], None] = app_config.set_secret,
        delete_secret: Callable[[str], None] = app_config.delete_secret,
    ) -> None:
        super().__init__()
        self._cfg_provider = cfg_provider
        self._get, self._set, self._delete = get_secret, set_secret, delete_secret
        self._needed: set[str] = set()
        self.rows: dict[str, KeyRow] = {}
        self._signals = _TestSignals()
        self._signals.done.connect(self._on_test_done)

        lay = QVBoxLayout(self)
        lay.addWidget(hint("金鑰存在 Windows 認證管理員，不會寫進設定檔，畫面只顯示末 4 碼。"
                           "貼上後按該列的「儲存」；「測試連線」會確認金鑰是否有效。"))
        for name, label in app_config.SECRET_NAMES.items():
            box = QGroupBox(label)
            v = QVBoxLayout(box)
            status = QLabel()
            edit = QLineEdit()
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.setPlaceholderText("貼上新的金鑰")
            show = QCheckBox("顯示")
            show.toggled.connect(lambda on, e=edit: e.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
            save = QPushButton("儲存")
            save.clicked.connect(lambda _=False, n=name: self.save_key(n))
            edit.returnPressed.connect(lambda n=name: self.save_key(n))
            test = QPushButton("測試連線")
            test.clicked.connect(lambda _=False, n=name: self.test_key(n))
            clear = QPushButton("清除")
            clear.clicked.connect(lambda _=False, n=name: self.clear_key(n))
            row = QHBoxLayout()
            row.addWidget(edit, 1)
            for w in (show, save, test, clear):
                row.addWidget(w)
            v.addWidget(status)
            v.addLayout(row)
            v.addWidget(hint(f"取得方式：{KEY_HELP.get(name, '')}"))
            lay.addWidget(box)
            self.rows[name] = KeyRow(name, status, edit, show, save, test, clear)
        lay.addStretch(1)
        self.refresh()

    def set_needed(self, needed: set[str]) -> None:
        """設定目前選的引擎需要哪些金鑰，並更新狀態顏色。"""
        self._needed = needed
        self.refresh()

    def refresh(self) -> None:
        """更新每把金鑰的狀態文字與顏色。"""
        for name, row in self.rows.items():
            value = self._get(name)
            if value:
                text, color = f"✔ 已設定（{app_config.mask_secret(value)}）", OK_COLOR
            elif name in self._needed:
                text, color = "✘ 尚未設定（目前選擇的引擎需要這把金鑰）", ERROR_COLOR
            else:
                text, color = "未設定（目前用不到）", "gray"
            row.status.setText(text)
            row.status.setStyleSheet(f"color: {color}; font-weight: bold;")

    def pending(self) -> list[str]:
        """已輸入但尚未儲存的金鑰。"""
        return [n for n, r in self.rows.items() if r.edit.text().strip()]

    def save_key(self, name: str, quiet: bool = False) -> bool:
        """儲存一把金鑰。"""
        row = self.rows[name]
        value = row.edit.text().strip()
        if not value:
            if not quiet:
                QMessageBox.information(self, "儲存金鑰", "請先貼上金鑰。")
            return False
        try:
            self._set(name, value)
        except Exception as exc:  # noqa: BLE001 - 顯示給使用者
            QMessageBox.critical(self, "儲存金鑰", f"儲存失敗：{exc}")
            return False
        row.edit.clear()
        row.show.setChecked(False)
        self.refresh()
        self.keys_changed.emit()
        return True

    def save_pending(self) -> list[str]:
        """儲存所有已輸入的金鑰，回傳成功的名稱。"""
        return [n for n in self.pending() if self.save_key(n, quiet=True)]

    def clear_key(self, name: str) -> None:
        """刪除已儲存的金鑰（先確認）。"""
        label = app_config.SECRET_NAMES[name]
        if not self._get(name):
            return
        answer = QMessageBox.question(self, "清除金鑰", f"確定要從 Windows 認證管理員刪除「{label}」嗎？")
        if answer == QMessageBox.StandardButton.Yes:
            self._delete(name)
            self.refresh()
            self.keys_changed.emit()

    def test_key(self, name: str) -> None:
        """在背景測試金鑰；有輸入新金鑰時測試新金鑰。"""
        row = self.rows[name]
        key = row.edit.text().strip() or self._get(name)
        if not key:
            QMessageBox.information(self, "測試連線", "請先貼上金鑰。")
            return
        cfg = self._cfg_provider()
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


class ServicesPage(QWidget):
    """「服務與費用」分頁：預設引擎、區域、模型、單價、預設語言、輸出資料夾。"""

    changed = Signal()

    def __init__(self, cfg: dict[str, Any], registry: LanguageRegistry) -> None:
        super().__init__()
        stt = cfg.get("stt", {})
        lay = QVBoxLayout(self)

        box = QGroupBox("語音轉文字引擎")
        form = QFormLayout()
        self.default_engine = QComboBox()
        for code, label in ENGINES:
            self.default_engine.addItem(label, code)
        self.default_engine.setCurrentIndex(max(0, self.default_engine.findData(stt.get("default_engine"))))
        self.default_engine.setToolTip("「轉錄」分頁預設使用的引擎。第 0 階段評估後再決定。")
        self.azure_region = QComboBox()
        self.azure_region.setEditable(True)
        self.azure_region.addItems(AZURE_REGIONS)
        self.azure_region.setCurrentText(stt.get("azure", {}).get("region", ""))
        self.azure_region.lineEdit().setPlaceholderText("請選擇或輸入 Azure 資源所在區域，例：eastasia")
        self.azure_endpoint = QLineEdit(stt.get("azure", {}).get("endpoint", ""))
        self.azure_endpoint.setPlaceholderText("選填，例：https://<資源名稱>.cognitiveservices.azure.com")
        self.openai_model = QLineEdit(stt.get("openai", {}).get("model", ""))
        self.soniox_model = QLineEdit(stt.get("soniox", {}).get("model", ""))
        self.soniox_model.setToolTip("Soniox 非同步模型名稱，例：stt-async-v6（官方改版時在此修改）")
        form.addRow("預設引擎：", self.default_engine)
        form.addRow("Azure 區域：", self.azure_region)
        form.addRow("Azure 自訂端點：", self.azure_endpoint)
        form.addRow("OpenAI 模型名稱：", self.openai_model)
        form.addRow("Soniox 模型名稱：", self.soniox_model)
        engine_box = QVBoxLayout(box)
        engine_box.addLayout(form)
        engine_box.addWidget(hint("Soniox 的中文輸出不保證是繁體：中文片段會先自動轉成臺灣繁體與臺灣用語，"
                                  "之後產生會議紀錄前，再由 AI 做臺灣在地化校潤。"))
        lay.addWidget(box)

        box = QGroupBox("API 單價（請依官方價目表填寫）")
        form = QFormLayout(box)
        self.azure_price = self._price_box(stt.get("azure", {}).get("price_per_hour", 0.0))
        self.openai_price = self._price_box(stt.get("openai", {}).get("price_per_hour", 0.0))
        self.soniox_price = self._price_box(stt.get("soniox", {}).get("price_per_hour", 0.0))
        self.cost = QLabel()
        self.cost.setWordWrap(True)
        form.addRow("Azure：", self.azure_price)
        form.addRow("OpenAI：", self.openai_price)
        form.addRow("Soniox：", self.soniox_price)
        form.addRow("2 小時會議約：", self.cost)
        lay.addWidget(box)

        box = QGroupBox("預設值")
        form = QFormLayout(box)
        self.languages = LanguagePicker(registry, cfg.get("languages", {}).get("default", ["zh", "en"]))
        self.output = FilePicker("dir", title="選擇輸出資料夾")
        self.output.set_path(app_config.output_dir(cfg))
        reset = QPushButton("還原預設")
        reset.clicked.connect(lambda: self.output.set_path(app_config.output_dir({})))
        open_btn = QPushButton("開啟資料夾")
        open_btn.clicked.connect(self._open_output)
        out_row = QHBoxLayout()
        out_row.addWidget(self.output, 1)
        out_row.addWidget(reset)
        out_row.addWidget(open_btn)
        form.addRow("會議語言：", self.languages)
        form.addRow("輸出資料夾：", out_row)
        lay.addWidget(box)
        lay.addStretch(1)

        for w in (self.azure_price, self.openai_price, self.soniox_price):
            w.valueChanged.connect(self._refresh_cost)
        self.default_engine.currentIndexChanged.connect(self.changed.emit)
        self._refresh_cost()

    @staticmethod
    def _price_box(value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 1000.0)
        spin.setDecimals(3)
        spin.setSingleStep(0.1)
        spin.setPrefix("US$ ")
        spin.setSuffix(" ／音訊小時")
        spin.setValue(float(value or 0.0))
        return spin

    def _refresh_cost(self) -> None:
        parts = []
        for label, spin in (("Azure", self.azure_price), ("OpenAI", self.openai_price),
                            ("Soniox", self.soniox_price)):
            parts.append(f"{label} US$ {spin.value() * 2:.2f}" if spin.value() else f"{label} 未設定單價")
        self.cost.setText("；".join(parts))

    def _open_output(self) -> None:
        path = self.output.path() or app_config.output_dir({})
        path.mkdir(parents=True, exist_ok=True)
        open_path(path)

    def engine(self) -> str:
        return str(self.default_engine.currentData())

    def updates(self) -> dict[str, Any]:
        """要寫入使用者設定檔的項目。"""
        out = self.output.path()
        default_out = app_config.output_dir({})
        return {
            "general": {"output_dir": "" if out is None or out == default_out else str(out)},
            "languages": {"default": self.languages.codes()},
            "stt": {
                "default_engine": self.engine(),
                "azure": {
                    "region": self.azure_region.currentText().strip(),
                    "endpoint": self.azure_endpoint.text().strip(),
                    "price_per_hour": round(self.azure_price.value(), 3),
                },
                "openai": {
                    "model": self.openai_model.text().strip(),
                    "price_per_hour": round(self.openai_price.value(), 3),
                },
                "soniox": {
                    "model": self.soniox_model.text().strip(),
                    "price_per_hour": round(self.soniox_price.value(), 3),
                },
            },
        }
