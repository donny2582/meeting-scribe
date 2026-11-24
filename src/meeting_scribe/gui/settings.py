"""「API 金鑰」分頁（SPEC 4.4）：只有兩個區塊。

- 語音轉錄：選擇服務（Azure／OpenAI／Soniox），只顯示該服務的金鑰與附加設定（區域、模型名稱）；
  與「轉錄」分頁的引擎同步。
- 文字整理處理：選擇服務（Anthropic／OpenAI），用於臺灣繁體在地化與之後的會議紀錄摘要。
- API 金鑰存在 Windows 認證管理員，畫面只顯示末 4 碼；狀態以顏色區分：
  綠＝已設定、紅＝目前選的服務需要但尚未設定、橘＝建議設定、灰＝目前用不到。
- 附加設定寫入使用者設定檔 ``%APPDATA%\\MeetingScribe\\config.toml``（只寫覆寫的項目）。
- 引擎、會議語言、輸出資料夾在「轉錄」分頁選擇，並記住上次的選擇；API 單價只在設定檔中調整。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import httpx
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
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
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from meeting_scribe import config as app_config
from meeting_scribe import models_catalog
from meeting_scribe.gui.widgets import ERROR_COLOR, OK_COLOR, WARN_COLOR, ModelPicker, hint

AZURE_REGIONS = ["eastasia", "southeastasia", "japaneast", "japanwest", "koreacentral",
                 "eastus", "westus2", "westeurope"]
ENGINES = (("azure", "Azure AI Speech"), ("openai", "OpenAI"), ("soniox", "Soniox"))
#: 語音轉錄服務：（代碼, 顯示名稱, 金鑰名稱）
STT_SERVICES = (("azure", "Azure AI Speech", "azure_speech_key"), ("openai", "OpenAI", "openai_api_key"),
                ("soniox", "Soniox", "soniox_api_key"))
#: 文字整理處理服務
LLM_SERVICES = (("anthropic", "Anthropic Claude", "anthropic_api_key"), ("openai", "OpenAI", "openai_api_key"))
KEY_HELP = {
    "azure_speech_key": "Azure 入口網站 → 語音服務資源 →「金鑰與端點」",
    "openai_api_key": "platform.openai.com → API keys",
    "soniox_api_key": "console.soniox.com → 專案 → API Keys",
    "anthropic_api_key": "console.anthropic.com → API Keys",
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
                    return False, "請先在下方選擇 Azure 區域"
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
    done = Signal(object, bool, str)


class _FetchSignals(QObject):
    done = Signal(str, object, str)


class KeyEditor(QWidget):
    """一把金鑰的輸入列：狀態、輸入框、顯示、儲存、測試連線、清除。"""

    def __init__(self, page: KeysPage, name: str) -> None:
        super().__init__()
        self.page, self.name = page, name
        self.status = QLabel()
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit.setPlaceholderText("貼上新的金鑰")
        self.reveal = QCheckBox("顯示")
        self.reveal.toggled.connect(lambda on: self.edit.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        self.save = QPushButton("儲存")
        self.save.clicked.connect(lambda: page.save_key(self))
        self.edit.returnPressed.connect(lambda: page.save_key(self))
        self.test = QPushButton("測試連線")
        self.test.clicked.connect(lambda: page.test_key(self))
        self.clear = QPushButton("清除")
        self.clear.clicked.connect(lambda: page.clear_key(self.name))
        row = QHBoxLayout()
        row.addWidget(self.edit, 1)
        for w in (self.reveal, self.save, self.test, self.clear):
            row.addWidget(w)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.status)
        lay.addLayout(row)


class KeysPage(QWidget):
    """「API 金鑰」分頁：語音轉錄、文字整理處理兩個區塊。"""

    keys_changed = Signal()
    stt_changed = Signal(str)
    llm_changed = Signal(str)

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
        self._recommended: dict[str, str] = {}
        self.editors: list[KeyEditor] = []
        self._signals = _TestSignals()
        self._signals.done.connect(self._on_test_done)
        cfg = cfg_provider()
        stt = cfg.get("stt", {})
        llm = cfg.get("llm", {})

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.addWidget(hint("只要設定你會用到的服務。金鑰存在 Windows 認證管理員，不會寫進設定檔，畫面只顯示末 4 碼。"))

        # ---- 區塊 1：語音轉錄 ----
        box = QGroupBox("1. 語音轉錄")
        v = QVBoxLayout(box)
        v.addWidget(hint("把錄音轉成逐字稿的服務；與「轉錄」分頁的引擎同步。"))
        self.stt_combo = QComboBox()
        for code, label, _ in STT_SERVICES:
            self.stt_combo.addItem(label, code)
        form = QFormLayout()
        form.addRow("服務：", self.stt_combo)
        v.addLayout(form)
        self.stt_stack = QStackedWidget()
        self.azure_region = QComboBox()
        self.azure_region.setEditable(True)
        self.azure_region.addItems(AZURE_REGIONS)
        self.azure_region.setCurrentText(stt.get("azure", {}).get("region", ""))
        self.azure_region.lineEdit().setPlaceholderText("申請 Azure 語音服務時選的區域，例：eastasia")
        self.azure_endpoint = QLineEdit(stt.get("azure", {}).get("endpoint", ""))
        self.azure_endpoint.setPlaceholderText("通常留空")
        self.custom: dict[str, list[str]] = {
            k: list(v) for k, v in cfg.get("models", {}).get("custom", {}).items()
        }
        self.fetched: dict[str, list[dict[str, str]]] = {
            k: list(v) for k, v in cfg.get("models", {}).get("fetched", {}).items()
        }
        self._fetch_signals = _FetchSignals()
        self._fetch_signals.done.connect(self._on_fetch_done)
        self._refresh_buttons: dict[str, QPushButton] = {}
        self._pickers: dict[str, list[ModelPicker]] = {}
        self.openai_model = self._picker("openai_stt", cfg, stt.get("openai", {}).get("model", ""))
        self.soniox_model = self._picker("soniox", cfg, stt.get("soniox", {}).get("model", ""))
        stt_extras: dict[str, list[tuple[str, QWidget]]] = {
            "azure": [("區域：", self.azure_region), ("自訂端點：", self.azure_endpoint)],
            "openai": [("語音轉錄模型：", self.openai_model), ("", self._refresh_button("openai_stt"))],
            "soniox": [("模型名稱：", self.soniox_model), ("", self._refresh_button("soniox"))],
        }
        stt_notes = {"soniox": "Soniox 的中文會先自動轉成繁體，再由下方「文字整理處理」的 AI 校潤成臺灣繁體中文與在地用語。"}
        for code, _label, key in STT_SERVICES:
            self.stt_stack.addWidget(self._service_page(key, stt_extras.get(code, []), stt_notes.get(code)))
        v.addWidget(self.stt_stack)
        lay.addWidget(box)

        # ---- 區塊 2：文字整理處理 ----
        box = QGroupBox("2. 文字整理處理")
        v = QVBoxLayout(box)
        v.addWidget(hint("用 AI 校潤、清理逐字稿並整理會議紀錄。不同工作可以用不同模型："
                         "簡單、字量大的工作用便宜的模型，最需要理解力的整理結論用較好的模型。費用為每場（約 2 小時）的估計值。"))
        self.llm_combo = QComboBox()
        for code, label, _ in LLM_SERVICES:
            self.llm_combo.addItem(label, code)
        form = QFormLayout()
        form.addRow("服務：", self.llm_combo)
        v.addLayout(form)
        self.llm_stack = QStackedWidget()
        self.shared_note = hint("與上方語音轉錄共用同一把 OpenAI 金鑰。")
        #: (服務, 工作) → 模型下拉選單
        self.task_models: dict[tuple[str, str], ModelPicker] = {}
        for code, _label, key in LLM_SERVICES:
            page = self._service_page(key, [], None)
            if code == "openai":
                page.layout().addWidget(self.shared_note)
            page.layout().addWidget(self._task_grid(code, cfg))
            self.llm_stack.addWidget(page)
        v.addWidget(self.llm_stack)
        lay.addWidget(box)

        # ---- 區塊 3：費用 ----
        lay.addWidget(self._budget_box(cfg))
        lay.addStretch(1)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setWidget(body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(area)

        self.stt_combo.currentIndexChanged.connect(self._on_stt_changed)
        self.llm_combo.currentIndexChanged.connect(self._on_llm_changed)
        self.set_stt(stt.get("default_engine", "azure"))
        idx = self.llm_combo.findData(llm.get("provider", "anthropic"))
        self.llm_combo.setCurrentIndex(max(idx, 0))
        self._on_llm_changed()
        self.refresh()

    def _picker(self, kind: str, cfg: dict[str, Any], value: str, recommended: str | None = None) -> ModelPicker:
        """建立模型下拉選單；同一類別的選單共用「＋」新增的模型。"""
        opts = [(o.id, o.label, o.note) for o in models_catalog.options(kind, cfg)]
        picker = ModelPicker(opts, recommended)
        picker.set_value(value)
        picker.added.connect(lambda model_id, k=kind: self._model_added(k, model_id))
        self._pickers.setdefault(kind, []).append(picker)
        return picker

    def _model_added(self, kind: str, model_id: str) -> None:
        """使用者按「＋」新增模型：記下來（儲存設定時寫入設定檔），並加到同類別的其他選單。"""
        lst = self.custom.setdefault(kind, [])
        if model_id not in lst:
            lst.append(model_id)
        for picker in self._pickers.get(kind, []):
            picker.add_option(model_id)

    def _budget_box(self, cfg: dict[str, Any]) -> QGroupBox:
        """「3. 費用」：每月上限、本月已使用、各服務明細、累計。"""
        box = QGroupBox("3. 費用")
        v = QVBoxLayout(box)
        v.addWidget(hint("費用由本程式依用量估算（語音轉錄＝錄音時數 × 每小時單價；文字整理＝tokens × 模型單價），"
                         "實際金額以各服務的帳單為準。達到上限後會停止新的轉錄與 AI 處理。"))
        form = QFormLayout()
        self.budget_limit = QDoubleSpinBox()
        self.budget_limit.setRange(0.0, 100000.0)
        self.budget_limit.setDecimals(2)
        self.budget_limit.setSingleStep(5.0)
        self.budget_limit.setPrefix("US$ ")
        self.budget_limit.setSpecialValueText("不限制")
        self.budget_limit.setValue(float(cfg.get("budget", {}).get("monthly_limit_usd", 0) or 0))
        self.budget_limit.setToolTip("每月最多花多少錢（美元）；0＝不限制。用到 80% 時會提醒。")
        self.budget_warn = float(cfg.get("budget", {}).get("warn_ratio", 0.8))
        form.addRow("每月費用上限：", self.budget_limit)
        v.addLayout(form)

        self.usage_label = QLabel()
        self.usage_label.setStyleSheet("font-weight: bold;")
        self.usage_bar = QProgressBar()
        self.usage_bar.setRange(0, 100)
        self.usage_bar.setTextVisible(False)
        self.usage_bar.setMaximumHeight(10)
        self.usage_detail = QLabel()
        self.usage_detail.setWordWrap(True)
        self.usage_total = hint("")
        v.addWidget(self.usage_label)
        v.addWidget(self.usage_bar)
        v.addWidget(self.usage_detail)
        v.addWidget(self.usage_total)
        row = QHBoxLayout()
        refresh = QPushButton("↻ 重新整理")
        refresh.clicked.connect(self.refresh_usage)
        ledger = QPushButton("開啟使用紀錄（Excel）")
        ledger.setToolTip("每一次呼叫的時間、服務、模型、用量與估計費用（不含逐字稿內容）")
        ledger.clicked.connect(self._open_ledger)
        row.addWidget(refresh)
        row.addWidget(ledger)
        row.addStretch(1)
        v.addLayout(row)
        self.budget_limit.valueChanged.connect(lambda _v: self.refresh_usage())
        self.refresh_usage()
        return box

    def refresh_usage(self) -> None:
        """重新讀取使用紀錄，更新本月已使用、明細與進度條。"""
        from meeting_scribe import usage

        summary = usage.summarize(usage.load_records())
        status = usage.BudgetStatus(self.budget_limit.value(), summary.month_cost, self.budget_warn)
        month = summary.month.replace("-", " 年 ") + " 月"
        if status.enabled:
            text = f"本月（{month}）已使用 US$ {status.used:.2f}／上限 US$ {status.limit:.2f}（{status.ratio:.0%}）"
            color = ERROR_COLOR if status.exceeded else (WARN_COLOR if status.warning else OK_COLOR)
            if status.exceeded:
                text += "　已達上限，新的工作會停止"
            elif status.warning:
                text += "　快到上限了"
        else:
            text, color = f"本月（{month}）已使用 US$ {status.used:.2f}（未設定上限）", OK_COLOR
        self.usage_label.setText(text)
        self.usage_label.setStyleSheet(f"font-weight: bold; color: {color};")
        self.usage_bar.setVisible(status.enabled)
        self.usage_bar.setValue(min(100, int(status.ratio * 100)))
        self.usage_bar.setStyleSheet(f"QProgressBar::chunk {{ background: {color}; }}")

        lines = []
        for g in summary.by_service:
            amount = f"{g.calls} 次" + (f"、{g.audio_sec / 3600:.1f} 小時" if g.kind == usage.KIND_STT
                                       else f"、{g.tokens:,} tokens")
            unpriced = "，未設定單價不計費" if g.unpriced == g.calls else ("，部分未設定單價" if g.unpriced else "")
            lines.append(f"・{g.kind}｜{g.service}：US$ {g.cost:.2f}（{amount}{unpriced}）")
        self.usage_detail.setText("\n".join(lines) if lines else "本月還沒有使用紀錄。")
        note = (f"本月有 {summary.unpriced_month} 筆用量沒有單價、未計入費用（例如 Azure、OpenAI 語音轉錄尚未設定每小時單價，"
                "可按「開啟設定檔（進階）」填寫 price_per_hour）。") if summary.unpriced_month else ""
        self.usage_total.setText(f"累計（全部紀錄）US$ {summary.total_cost:.2f}。{note}")

    def _open_ledger(self) -> None:
        from meeting_scribe import usage
        from meeting_scribe.gui.widgets import open_path

        path = usage.ledger_path()
        if not path.exists():
            QMessageBox.information(self, "使用紀錄", "還沒有使用紀錄，轉錄或 AI 處理後就會產生。")
            return
        open_path(path)

    def _refresh_button(self, kind: str) -> QPushButton:
        """「↻ 更新模型清單」：用已儲存（或剛貼上）的金鑰向服務查詢目前可用的模型。"""
        btn = QPushButton("↻ 從服務更新模型清單")
        btn.setToolTip("用官方的「列出模型」API 取得目前可用的模型，加到下拉選單（需要先設定金鑰）")
        btn.clicked.connect(lambda: self.refresh_models(kind))
        btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._refresh_buttons[kind] = btn
        return btn

    def refresh_models(self, kind: str) -> None:
        """在背景查詢模型清單。"""
        key_name = models_catalog.FETCHABLE[kind]
        typed = next((e.edit.text().strip() for e in self.editors if e.name == key_name and e.edit.text().strip()),
                     "")
        key = typed or self._get(key_name)
        if not key:
            QMessageBox.information(self, "更新模型清單", f"請先設定「{app_config.SECRET_NAMES[key_name]}」。")
            return
        btn = self._refresh_buttons.get(kind)
        if btn:
            btn.setEnabled(False)
            btn.setText("查詢中…")
        cfg = self._cfg_provider()

        def work() -> None:
            try:
                found = models_catalog.fetch_models(kind, key, cfg)
            except models_catalog.FetchError as exc:
                self._fetch_signals.done.emit(kind, None, str(exc))
            else:
                self._fetch_signals.done.emit(kind, found, "")

        threading.Thread(target=work, daemon=True).start()

    def _on_fetch_done(self, kind: str, found: list[models_catalog.ModelOption] | None, error: str) -> None:
        btn = self._refresh_buttons.get(kind)
        if btn:
            btn.setEnabled(True)
            btn.setText("↻ 從服務更新模型清單")
        if found is None:
            QMessageBox.warning(self, "更新模型清單", error)
            return
        added = 0
        for opt in found:
            if all(not p.has(opt.id) for p in self._pickers.get(kind, [])):
                added += 1
            for picker in self._pickers.get(kind, []):
                picker.add_option(opt.id, opt.note)
        self.fetched[kind] = [{"id": o.id, "label": o.label} for o in found]
        msg = f"服務目前提供 {len(found)} 個模型，新增 {added} 個到下拉選單。" if found else "服務沒有回傳可用的模型。"
        QMessageBox.information(self, "更新模型清單", msg + "\n按右上角「儲存程式設定」後會記住這份清單。")

    def _task_grid(self, provider: str, cfg: dict[str, Any]) -> QWidget:
        """「每項工作用哪個模型」：工作名稱｜模型下拉選單＋「＋」，下方灰字為建議與每場費用。"""
        box = QWidget()
        grid = QGridLayout(box)
        grid.setContentsMargins(0, 6, 0, 0)
        grid.setColumnStretch(1, 1)
        title = QLabel("各項工作使用的模型")
        title.setStyleSheet("font-weight: bold;")
        head = QHBoxLayout()
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self._refresh_button(provider))
        grid.addLayout(head, 0, 0, 1, 2)
        selected = cfg.get("llm", {}).get(provider, {}).get("models", {})
        labels = {o.id: o.label for o in models_catalog.builtin_options(provider)}
        row = 1
        for task in models_catalog.tasks():
            rec = task.recommend.get(provider)
            picker = self._picker(provider, cfg, selected.get(task.code) or rec or "", rec)
            self.task_models[(provider, task.code)] = picker
            grid.addWidget(QLabel(f"{task.label}："), row, 0)
            grid.addWidget(picker, row, 1)
            rec_text = f"建議 {labels.get(rec, rec)}｜每場 US$ {task.cost}" if rec else f"每場 US$ {task.cost}"
            note = hint(f"{rec_text}。{task.reason}")
            grid.addWidget(note, row + 1, 1)
            row += 2
        return box

    def _service_page(self, key: str, extras: list[tuple[str, QWidget]], note: str | None) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        editor = KeyEditor(self, key)
        self.editors.append(editor)
        v.addWidget(editor)
        if extras:
            form = QFormLayout()
            for text, widget in extras:
                form.addRow(text, widget)
            v.addLayout(form)
        v.addWidget(hint(f"取得方式：{KEY_HELP.get(key, '')}"))
        if note:
            v.addWidget(hint(note))
        return page

    # ---- 服務選擇 ----
    def stt_provider(self) -> str:
        return str(self.stt_combo.currentData())

    def llm_provider(self) -> str:
        return str(self.llm_combo.currentData())

    def set_stt(self, code: str) -> None:
        """切換語音轉錄服務（不發出 stt_changed，避免與「轉錄」分頁互相觸發）。"""
        idx = self.stt_combo.findData(code)
        if idx < 0:
            return
        self.stt_combo.blockSignals(True)
        self.stt_combo.setCurrentIndex(idx)
        self.stt_combo.blockSignals(False)
        self.stt_stack.setCurrentIndex(idx)
        self._update_shared_note()

    def _on_stt_changed(self, _i: int = 0) -> None:
        self.stt_stack.setCurrentIndex(self.stt_combo.currentIndex())
        self._update_shared_note()
        self.stt_changed.emit(self.stt_provider())

    def _on_llm_changed(self, _i: int = 0) -> None:
        self.llm_stack.setCurrentIndex(self.llm_combo.currentIndex())
        self.llm_changed.emit(self.llm_provider())

    def _update_shared_note(self) -> None:
        self.shared_note.setVisible(self.stt_provider() == "openai")

    def stt_editor(self) -> KeyEditor:
        return self.editors[self.stt_combo.currentIndex()]

    def llm_editor(self) -> KeyEditor:
        return self.editors[len(STT_SERVICES) + self.llm_combo.currentIndex()]

    def editor_for(self, name: str) -> KeyEditor:
        """某把金鑰的輸入列（優先回傳目前顯示中的那一個）。"""
        for editor in (self.stt_editor(), self.llm_editor()):
            if editor.name == name:
                return editor
        return next(e for e in self.editors if e.name == name)

    # ---- 狀態 ----
    def set_needed(self, needed: set[str], recommended: dict[str, str] | None = None) -> None:
        """設定必要（紅）與建議（橘，名稱 → 原因）的金鑰，並更新狀態顏色。"""
        self._needed = needed
        self._recommended = recommended or {}
        self.refresh()

    def refresh(self) -> None:
        """更新每一列的狀態文字與顏色。"""
        for editor in self.editors:
            value = self._get(editor.name)
            if value:
                text, color = f"✔ 已設定（{app_config.mask_secret(value)}）", OK_COLOR
            elif editor.name in self._needed:
                text, color = "✘ 尚未設定（目前選擇的服務需要這把金鑰）", ERROR_COLOR
            elif editor.name in self._recommended:
                text, color = f"建議設定（{self._recommended[editor.name]}）", WARN_COLOR
            else:
                text, color = "未設定", "gray"
            editor.status.setText(text)
            editor.status.setStyleSheet(f"color: {color}; font-weight: bold;")

    # ---- 金鑰操作 ----
    def pending(self) -> list[str]:
        """已輸入但尚未儲存的金鑰名稱。"""
        return list(dict.fromkeys(e.name for e in self.editors if e.edit.text().strip()))

    def save_key(self, editor: KeyEditor, quiet: bool = False) -> bool:
        """儲存一把金鑰。"""
        value = editor.edit.text().strip()
        if not value:
            if not quiet:
                QMessageBox.information(self, "儲存金鑰", "請先貼上金鑰。")
            return False
        try:
            self._set(editor.name, value)
        except Exception as exc:  # noqa: BLE001 - 顯示給使用者
            QMessageBox.critical(self, "儲存金鑰", f"儲存失敗：{exc}")
            return False
        editor.edit.clear()
        editor.reveal.setChecked(False)
        self.refresh()
        self.keys_changed.emit()
        return True

    def save_pending(self) -> list[str]:
        """儲存所有已輸入的金鑰，回傳成功的名稱。"""
        return [e.name for e in self.editors if e.edit.text().strip() and self.save_key(e, quiet=True)]

    def clear_key(self, name: str) -> None:
        """刪除已儲存的金鑰（先確認）。"""
        if not self._get(name):
            return
        label = app_config.SECRET_NAMES[name]
        answer = QMessageBox.question(self, "清除金鑰", f"確定要從 Windows 認證管理員刪除「{label}」嗎？")
        if answer == QMessageBox.StandardButton.Yes:
            self._delete(name)
            self.refresh()
            self.keys_changed.emit()

    def test_key(self, editor: KeyEditor) -> None:
        """在背景測試金鑰；有輸入新金鑰時測試新金鑰。"""
        key = editor.edit.text().strip() or self._get(editor.name)
        if not key:
            QMessageBox.information(self, "測試連線", "請先貼上金鑰。")
            return
        cfg = app_config._deep_merge(self._cfg_provider(), self.updates())  # 用畫面上尚未儲存的值
        editor.test.setEnabled(False)
        editor.test.setText("測試中…")

        def work() -> None:
            ok, msg = check_key(editor.name, key, cfg)
            self._signals.done.emit(editor, ok, msg)

        threading.Thread(target=work, daemon=True).start()

    def _on_test_done(self, editor: KeyEditor, ok: bool, msg: str) -> None:
        editor.test.setEnabled(True)
        editor.test.setText("測試連線")
        label = app_config.SECRET_NAMES[editor.name]
        if ok:
            QMessageBox.information(self, "測試連線", f"{label}：{msg}")
        else:
            QMessageBox.warning(self, "測試連線", f"{label}：{msg}")

    # ---- 設定 ----
    def has_azure_location(self) -> bool:
        return bool(self.azure_region.currentText().strip() or self.azure_endpoint.text().strip())

    def updates(self) -> dict[str, Any]:
        """要寫入使用者設定檔的項目。"""
        return {
            "stt": {
                "default_engine": self.stt_provider(),
                "azure": {"region": self.azure_region.currentText().strip(),
                          "endpoint": self.azure_endpoint.text().strip()},
                "openai": {"model": self.openai_model.value()},
                "soniox": {"model": self.soniox_model.value()},
            },
            "llm": {
                "provider": self.llm_provider(),
                **{p: {"models": {t: pk.value() for (pp, t), pk in self.task_models.items() if pp == p}}
                   for p, _l, _k in LLM_SERVICES},
            },
            "budget": {"monthly_limit_usd": round(self.budget_limit.value(), 2)},
            "models": {"custom": {k: list(v) for k, v in self.custom.items()},
                       "fetched": {k: list(v) for k, v in self.fetched.items()}},
        }
