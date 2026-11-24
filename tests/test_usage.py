"""使用量與費用估算、每月上限（使用者資料已由 conftest 隔離到暫存目錄）。"""

import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from meeting_scribe import usage
from meeting_scribe.config import load_config
from meeting_scribe.models_catalog import token_price


def test_token_prices_from_catalog() -> None:
    assert token_price("claude-haiku-5-5") == (0.10, 0.50)
    assert token_price("claude-opus-5-5") == (4.0, 20.0)
    assert token_price("gpt-6.2-sol") == (2.0, 10.0)
    assert token_price("my-custom-model") is None


def test_record_and_summarize() -> None:
    stt = usage.record_stt("Soniox", "Soniox stt-async-v6", 7200, 0.10)
    assert stt.cost == pytest.approx(0.20)
    llm = usage.record_llm("Anthropic", "claude-opus-5-5", "localize", 50_000, 50_000)
    assert llm.cost == pytest.approx(0.2 + 1.0)
    unknown = usage.record_stt("Azure", "Azure", 3600, 0.0)
    assert unknown.cost is None
    usage.record_llm("OpenAI", "custom-x", "filler", 10, 10)

    records = usage.load_records()
    assert len(records) == 4
    s = usage.summarize(records)
    assert s.month_cost == pytest.approx(1.40) and s.total_cost == pytest.approx(1.40)
    assert s.unpriced_month == 2
    first = s.by_service[0]
    assert first.kind == usage.KIND_STT  # 語音轉錄排前面
    text = usage.ledger_path().read_text(encoding="utf-8-sig")
    assert text.startswith("時間,類別,服務") and "localize" in text  # 不含任何逐字稿內容


def test_summary_separates_months(tmp_path: Path) -> None:
    old = usage.UsageRecord(datetime(2026, 10, 3, 9, 0), usage.KIND_LLM, "Anthropic", "m", cost=5.0)
    now = usage.UsageRecord(datetime(2026, 11, 3, 9, 0), usage.KIND_LLM, "Anthropic", "m", cost=1.5)
    s = usage.summarize([old, now], now=datetime(2026, 11, 20))
    assert s.month == "2026-11" and s.month_cost == 1.5 and s.total_cost == 6.5


def test_budget_status_and_check() -> None:
    cfg = load_config()
    assert not usage.budget_status(cfg).enabled  # 預設不限制
    usage.record_llm("Anthropic", "claude-opus-5-5", "localize", 0, 450_000)  # US$ 9.00
    cfg["budget"]["monthly_limit_usd"] = 10.0
    status = usage.budget_status(cfg)
    assert status.warning and not status.exceeded and status.ratio == pytest.approx(0.9)
    usage.record_llm("Anthropic", "claude-opus-5-5", "localize", 0, 100_000)  # 再 US$ 2.00
    with pytest.raises(usage.BudgetExceeded, match="已達上限"):
        usage.check_budget(cfg)


def test_transcribe_blocked_when_over_budget(tmp_path: Path) -> None:
    from meeting_scribe import tasks
    from meeting_scribe.postprocess.lang_detect import LanguageRegistry

    cfg = load_config()
    cfg["budget"]["monthly_limit_usd"] = 1.0
    usage.record_llm("Anthropic", "claude-opus-5-5", "summary", 0, 100_000)  # US$ 2.00
    with pytest.raises(usage.BudgetExceeded):
        tasks.transcribe_audio("soniox", tmp_path / "a.m4a", ["zh"], [], cfg, LanguageRegistry.load(), tmp_path)


def test_llm_calls_are_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    from meeting_scribe.llm.anthropic_llm import AnthropicLLM

    llm = AnthropicLLM(load_config(), api_key="sk-ant-test")
    response = SimpleNamespace(stop_reason="end_turn", model="claude-haiku-5-5",
                               content=[SimpleNamespace(type="text", text='{"items": []}')],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=2000,
                                                     cache_creation_input_tokens=0, cache_read_input_tokens=500))
    fake = SimpleNamespace(create=lambda **k: response)
    llm.client = SimpleNamespace(beta=SimpleNamespace(messages=fake), messages=fake)  # type: ignore[assignment]
    llm.complete_json("s", "u", {}, "filler")
    rec = usage.load_records()[-1]
    assert (rec.service, rec.model, rec.task, rec.input_tokens, rec.output_tokens) == (
        "Anthropic", "claude-haiku-5-5", "filler", 1500, 2000)
    assert rec.cost == pytest.approx((1500 * 0.10 + 2000 * 0.50) / 1_000_000)


# ---- 介面 ----
@pytest.fixture
def win(monkeypatch: pytest.MonkeyPatch) -> Any:
    from meeting_scribe.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    asked: list[str] = []
    for kind in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, kind,
                            lambda *a, **k: asked.append(str(a[1]) if len(a) > 1 else "") or QMessageBox.StandardButton.Yes)
    store = {"soniox_api_key": "sx"}
    monkeypatch.setattr("meeting_scribe.tasks.get_secret", store.get)
    w = MainWindow(secret_store={"get_secret": store.get, "set_secret": store.__setitem__,
                                 "delete_secret": lambda n: None})
    w.asked = asked  # type: ignore[attr-defined]
    yield w
    w.close()
    del app


def test_cost_section_shows_usage(win: Any) -> None:
    keys = win.keys
    assert "還沒有使用紀錄" in keys.usage_detail.text()
    usage.record_stt("Soniox", "Soniox stt-async-v6", 3 * 3600, 0.10)
    usage.record_llm("Anthropic", "claude-sonnet-5-5", "summary", 20_000, 5_000)
    keys.budget_limit.setValue(1.0)  # 會觸發重新整理
    # 0.30（3 小時 × 0.10）＋ 0.04（2 萬輸入 × 2）＋ 0.05（5 千輸出 × 10）
    assert "US$ 0.39／上限 US$ 1.00（39%）" in keys.usage_label.text()
    assert "Soniox：US$ 0.30（1 次、3.0 小時）" in keys.usage_detail.text()
    assert "25,000 tokens" in keys.usage_detail.text()
    assert not keys.usage_bar.isHidden() and keys.usage_bar.value() == 39
    keys.budget_limit.setValue(0.35)
    assert "已達上限" in keys.usage_label.text()
    assert win.save_settings(quiet=True)
    assert load_config()["budget"]["monthly_limit_usd"] == 0.35


def test_run_blocked_over_limit(win: Any) -> None:
    usage.record_llm("Anthropic", "claude-opus-5-5", "summary", 0, 100_000)  # US$ 2.00
    win.keys.budget_limit.setValue(1.0)
    win.go_to("transcribe")
    assert win.prepare_run(["soniox"]) is None
    assert win.tabs.currentWidget() is win.keys
    assert any("已達費用上限" in a for a in win.asked)
