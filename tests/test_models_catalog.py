"""模型清單、各工作的模型下拉選單、「＋」新增、從服務更新模型清單。"""

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from meeting_scribe import config as app_config
from meeting_scribe import models_catalog, tasks
from meeting_scribe.config import load_config
from meeting_scribe.gui.main_window import MainWindow
from meeting_scribe.models_catalog import FetchError, fetch_models, options


def test_catalog_tasks_and_recommendations() -> None:
    task_list = models_catalog.tasks()
    assert [t.code for t in task_list] == ["localize", "filler", "translate", "summary"]
    by_code = {t.code: t for t in task_list}
    assert by_code["filler"].recommend == {"anthropic": "claude-haiku-5-5", "openai": "gpt-6-luna"}
    assert by_code["translate"].recommend["anthropic"] == "claude-sonnet-5-5"
    assert by_code["summary"].recommend["anthropic"] == "claude-sonnet-5-5"
    assert "0.03" in by_code["filler"].cost
    cfg = load_config()
    for t in task_list:  # 預設設定採用建議模型
        assert cfg["llm"]["anthropic"]["models"][t.code] == t.recommend["anthropic"]
        assert cfg["llm"]["openai"]["models"][t.code] == t.recommend["openai"]


def test_options_merge_fetched_and_custom() -> None:
    cfg = {"models": {"custom": {"anthropic": ["claude-x-9", "claude-haiku-5-5"]},
                      "fetched": {"anthropic": [{"id": "claude-new-6", "label": "Claude New 6"}]}}}
    ids = [o.id for o in options("anthropic", cfg)]
    assert ids[:3] == ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]
    assert ids[3:] == ["claude-new-6", "claude-x-9"]  # 不重複


def test_fetch_openai_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    data = {"data": [{"id": i} for i in ("gpt-6.2-sol", "gpt-4o-transcribe-diarize", "text-embedding-3",
                                         "gpt-6-luna", "gpt-image-2", "tts-1", "gpt-realtime")]}
    monkeypatch.setattr(httpx, "get", lambda url, **k: httpx.Response(200, json=data, request=httpx.Request("GET", url)))
    assert [o.id for o in fetch_models("openai", "k", load_config())] == ["gpt-6-luna", "gpt-6.2-sol"]
    assert [o.id for o in fetch_models("openai_stt", "k", load_config())] == ["gpt-4o-transcribe-diarize"]


def test_fetch_soniox_async_only(monkeypatch: pytest.MonkeyPatch) -> None:
    data = {"models": [{"id": "stt-async-v6", "name": "Async v6", "transcription_mode": "async"},
                       {"id": "stt-rt-v6", "transcription_mode": "real_time"}]}
    monkeypatch.setattr(httpx, "get", lambda url, **k: httpx.Response(200, json=data, request=httpx.Request("GET", url)))
    assert [o.id for o in fetch_models("soniox", "k", load_config())] == ["stt-async-v6"]


def test_fetch_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    import anthropic

    models = [SimpleNamespace(id="claude-opus-5-5", display_name="Claude Opus 5.5")]
    monkeypatch.setattr(anthropic, "Anthropic",
                        lambda **k: SimpleNamespace(models=SimpleNamespace(list=lambda: iter(models))))
    found = fetch_models("anthropic", "k", load_config())
    assert [(o.id, o.label) for o in found] == [("claude-opus-5-5", "Claude Opus 5.5")]


def test_fetch_bad_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda url, **k: httpx.Response(401, request=httpx.Request("GET", url)))
    with pytest.raises(FetchError, match="金鑰"):
        fetch_models("soniox", "bad", load_config())


# ---- 介面 ----
@pytest.fixture
def win(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    store = {"anthropic_api_key": "sk-ant-1234"}
    monkeypatch.setattr(tasks, "get_secret", store.get)
    for kind in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, kind, lambda *a, **k: QMessageBox.StandardButton.Yes)
    w = MainWindow(secret_store={"get_secret": store.get, "set_secret": store.__setitem__,
                                 "delete_secret": lambda n: store.pop(n, None)})
    yield w
    w.close()
    del app


def test_task_pickers_default_and_save(win: Any) -> None:
    keys = win.keys
    filler = keys.task_models[("anthropic", "filler")]
    assert filler.value() == "claude-haiku-5-5"
    assert "★ 建議" in filler.combo.currentText()
    summary = keys.task_models[("anthropic", "summary")]
    summary.set_value("claude-opus-5-5")  # 重要會議改用 Opus
    assert win.save_settings(quiet=True)
    saved = app_config.load_config()["llm"]["anthropic"]["models"]
    assert saved["summary"] == "claude-opus-5-5" and saved["filler"] == "claude-haiku-5-5"


def test_plus_adds_model_to_all_pickers(win: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("claude-sonnet-6", True))
    keys = win.keys
    picker = keys.task_models[("anthropic", "translate")]
    picker.plus.click()
    assert picker.value() == "claude-sonnet-6"
    assert keys.task_models[("anthropic", "summary")].has("claude-sonnet-6")  # 同服務的其他工作也看得到
    assert not keys.task_models[("openai", "summary")].has("claude-sonnet-6")  # 不同服務不受影響
    assert win.save_settings(quiet=True)
    cfg = app_config.load_config()
    assert cfg["models"]["custom"]["anthropic"] == ["claude-sonnet-6"]
    assert cfg["llm"]["anthropic"]["models"]["translate"] == "claude-sonnet-6"


def test_plus_rejects_spaces(win: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("bad name", True))
    picker = win.keys.task_models[("anthropic", "translate")]
    before = picker.combo.count()
    picker.ask_new()
    assert picker.combo.count() == before


def test_refresh_models_from_service(win: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    found = [models_catalog.ModelOption("claude-opus-6", "Claude Opus 6", "從 Anthropic 取得")]
    monkeypatch.setattr(models_catalog, "fetch_models", lambda kind, key, cfg: found)
    win.keys.refresh_models("anthropic")
    for _ in range(100):
        QCoreApplication.processEvents()
        if win.keys._refresh_buttons["anthropic"].isEnabled():
            break
        QTest.qWait(10)
    assert all(win.keys.task_models[("anthropic", t)].has("claude-opus-6") for t in models_catalog.TASKS)
    assert win.save_settings(quiet=True)
    assert app_config.load_config()["models"]["fetched"]["anthropic"][0]["id"] == "claude-opus-6"
