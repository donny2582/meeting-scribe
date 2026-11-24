"""圖形介面（離屏測試，不碰真實的 Windows 認證管理員、網路與 API）。"""

import os
import tomllib
from pathlib import Path
from typing import Any

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit, QMessageBox

from meeting_scribe import config as app_config
from meeting_scribe import tasks
from meeting_scribe.gui import settings as gui_settings
from meeting_scribe.gui.main_window import MainWindow
from meeting_scribe.models import SttResult


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """隔離 APPDATA、金鑰與對話框。"""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    for name in app_config.SECRET_NAMES:
        monkeypatch.delenv(f"MEETINGSCRIBE_{name.upper()}", raising=False)
    store: dict[str, str] = {"openai_api_key": "sk-old-9999"}
    monkeypatch.setattr(tasks, "get_secret", store.get)
    dialogs: list[str] = []

    def record(kind: str) -> Any:
        def fn(*args: Any, **kwargs: Any) -> Any:
            dialogs.append(f"{kind}:{args[1] if len(args) > 1 else ''}")
            return QMessageBox.StandardButton.Yes
        return fn

    for kind in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, kind, record(kind))
    return {"store": store, "dialogs": dialogs, "tmp": tmp_path}


def _window(env: dict[str, Any]) -> MainWindow:
    store = env["store"]
    return MainWindow(secret_store={
        "get_secret": store.get, "set_secret": store.__setitem__, "delete_secret": lambda n: store.pop(n, None),
    })


def test_first_run_opens_keys_tab(app: QApplication, env: dict[str, Any]) -> None:
    win = _window(env)  # 預設引擎 azure 沒有金鑰
    assert win.tabs.currentWidget() is win.keys
    assert "尚未設定" in win.keys.editor_for("azure_speech_key").status.text()
    assert "已設定" in win.keys.editor_for("openai_api_key").status.text()
    assert win.keys.editor_for("anthropic_api_key").status.text() == "未設定"
    win.close()


def test_save_key_and_settings(app: QApplication, env: dict[str, Any]) -> None:
    win = _window(env)
    win.keys.editor_for("azure_speech_key").edit.setText("  az-new-key-1234  ")
    win.keys.azure_region.setCurrentText("eastasia")
    win.keys.soniox_model.set_value("stt-async-v7")
    win.transcribe.engine.setCurrentIndex(win.transcribe.engine.findData("soniox"))
    win.transcribe.languages.set_codes(["zh", "ja"])
    assert win.save_settings(quiet=True)
    assert env["store"]["azure_speech_key"] == "az-new-key-1234"
    assert env["store"]["openai_api_key"] == "sk-old-9999"
    assert "****1234" in win.keys.editor_for("azure_speech_key").status.text()
    with open(env["tmp"] / "appdata" / "MeetingScribe" / "config.toml", "rb") as f:
        saved = tomllib.load(f)
    assert saved["stt"]["azure"]["region"] == "eastasia"
    assert saved["stt"]["soniox"]["model"] == "stt-async-v7"
    assert saved["stt"]["default_engine"] == "soniox"  # 記住轉錄分頁的引擎
    assert saved["ui"]["last_languages"] == ["zh", "ja"]
    assert saved["general"]["output_dir"] == ""  # 預設資料夾不寫入
    assert "price_per_hour" not in saved["stt"]["azure"]  # 單價不由介面覆寫
    win.close()

    again = _window(env)  # 重新開啟時沿用上次的選擇
    assert again.transcribe.engine.currentData() == "soniox"
    assert again.transcribe.languages.codes() == ["zh", "ja"]
    assert again.keys.azure_region.currentText() == "eastasia"
    again.close()


def test_two_sections_and_sync(app: QApplication, env: dict[str, Any]) -> None:
    win = _window(env)
    keys = win.keys
    # 「轉錄」換引擎 → 語音轉錄區塊同步，只顯示該服務的金鑰
    win.transcribe.engine.setCurrentIndex(win.transcribe.engine.findData("soniox"))
    assert keys.stt_provider() == "soniox"
    assert keys.stt_editor().name == "soniox_api_key"
    assert "尚未設定" in keys.stt_editor().status.text()
    assert "建議設定" in keys.llm_editor().status.text()  # Soniox 需要文字整理做在地化
    # 語音轉錄區塊換服務 → 「轉錄」分頁同步
    keys.stt_combo.setCurrentIndex(keys.stt_combo.findData("openai"))
    assert win.transcribe.engine.currentData() == "openai"
    # 文字整理處理改用 OpenAI：與語音轉錄共用同一把金鑰
    keys.llm_combo.setCurrentIndex(keys.llm_combo.findData("openai"))
    assert keys.llm_editor().name == "openai_api_key"
    assert not keys.shared_note.isHidden()
    keys.llm_editor().edit.setText("sk-new-5678")
    keys.save_key(keys.llm_editor())
    assert "****5678" in keys.stt_editor().status.text()  # 兩個區塊同時更新
    assert win.save_settings(quiet=True)
    assert app_config.load_config()["llm"]["provider"] == "openai"
    win.close()


def test_no_services_tab(app: QApplication, env: dict[str, Any]) -> None:
    win = _window(env)
    labels = [win.tabs.tabText(i) for i in range(win.tabs.count())]
    assert labels == ["轉錄", "引擎評估", "詞庫", "API 金鑰"]
    win.close()


def test_password_hidden_and_clear(app: QApplication, env: dict[str, Any]) -> None:
    win = _window(env)
    row = win.keys.editor_for("openai_api_key")
    assert row.edit.echoMode() == QLineEdit.EchoMode.Password
    row.reveal.setChecked(True)
    assert row.edit.echoMode() == QLineEdit.EchoMode.Normal
    win.keys.clear_key("openai_api_key")
    assert "openai_api_key" not in env["store"]
    win.close()


def test_run_blocked_without_key(app: QApplication, env: dict[str, Any]) -> None:
    win = _window(env)
    win.go_to("transcribe")
    assert win.prepare_run(["azure"]) is None
    assert win.tabs.currentWidget() is win.keys
    assert any("缺少 API 金鑰" in d for d in env["dialogs"])
    win.close()


def test_run_blocked_without_region(app: QApplication, env: dict[str, Any]) -> None:
    env["store"]["azure_speech_key"] = "k"
    win = _window(env)
    win.go_to("transcribe")
    assert win.prepare_run(["azure"]) is None
    assert win.tabs.currentWidget() is win.keys
    assert any("缺少 Azure 區域" in d for d in env["dialogs"])
    win.close()


def test_transcribe_runs_in_background(app: QApplication, env: dict[str, Any],
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    audio = env["tmp"] / "meeting.m4a"
    audio.write_bytes(b"x")
    win = _window(env)
    monkeypatch.setattr(win, "prepare_run", lambda engines: win.cfg)
    calls: list[tuple] = []

    def fake_job(engine, audio_path, langs, glossaries, cfg, registry, out_root, speakers, progress, cancel):  # type: ignore[no-untyped-def]
        calls.append((engine, langs, speakers))
        progress(50, "語音轉文字中")
        folder = out_root / "job"
        folder.mkdir(parents=True)
        result = SttResult(engine=engine, engine_version="v", audio_duration=60, elapsed_sec=3,
                           locales=["zh-TW"], segments=[])
        return tasks.TranscribeOutput(folder, folder / "a.json", folder / "a.txt", result)

    monkeypatch.setattr(tasks, "run_transcribe_job", fake_job)
    page = win.transcribe
    page.audio.set_path(audio)
    page.output.set_path(env["tmp"] / "out")
    page.languages.set_codes(["zh", "ja"])
    page.speakers.setValue(5)
    page.start()
    assert not page.run_button.isEnabled()  # 執行中停用
    for _ in range(100):
        QCoreApplication.processEvents()
        if not page.panel.running:
            break
        QTest.qWait(20)
    assert not page.panel.running
    assert calls == [("azure", ["zh", "ja"], 5)]
    assert page.open_folder.isEnabled() and page.run_button.isEnabled()
    assert page.panel.status.text() == "完成"
    win.close()


def test_transcribe_requires_audio(app: QApplication, env: dict[str, Any]) -> None:
    win = _window(env)
    win.transcribe.start()
    assert any("請先選擇錄音檔" in d or "開始轉錄" in d for d in env["dialogs"])
    assert not win.transcribe.panel.running
    win.close()


def test_benchmark_lists_missing_inputs(app: QApplication, env: dict[str, Any]) -> None:
    win = _window(env)
    win.benchmark.start()
    assert any("開始評估" in d for d in env["dialogs"])
    assert not win.benchmark.panel.running
    win.close()


def test_glossary_seeded_and_new(app: QApplication, env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    win = _window(env)
    assert win.glossary.table.rowCount() == 1  # 初始詞庫已複製
    assert win.transcribe.glossaries.count() == 1
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("共用", True))
    win.glossary.new_glossary()
    assert win.glossary.table.rowCount() == 2
    assert win.transcribe.glossaries.count() == 2  # 轉錄分頁同步更新
    win.close()


def test_default_glossary_cannot_be_deleted(app: QApplication, env: dict[str, Any],
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    from meeting_scribe.storage import trash

    trashed: list[Path] = []
    monkeypatch.setattr(trash, "move_to_recycle_bin", lambda p: (trashed.append(p), p.unlink()))
    win = _window(env)
    page = win.glossary
    page.table.selectRow(0)
    assert page.selected().stem == "道安業務"
    assert "預設" in page.table.item(0, 1).text()
    assert not page.delete_button.isEnabled()
    page.delete_selected()  # 即使直接呼叫也不會刪
    assert trashed == [] and page.selected().exists()

    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("共用", True))
    page.new_glossary()
    row = next(r for r in range(page.table.rowCount()) if page.table.item(r, 0).text() == "共用")
    page.table.selectRow(row)
    assert page.delete_button.isEnabled()
    page.delete_selected()
    assert [p.stem for p in trashed] == ["共用"]
    assert page.table.rowCount() == 1
    assert win.transcribe.glossaries.count() == 1
    win.close()


def test_default_glossary_restored_if_missing(env: dict[str, Any]) -> None:
    path = app_config.user_glossary_dir() / "道安業務.csv"
    path.write_text("正確詞\n自訂詞\n", encoding="utf-8-sig")
    app_config.user_glossary_dir()
    assert "自訂詞" in path.read_text(encoding="utf-8-sig")  # 已存在時不覆蓋
    path.unlink()
    app_config.user_glossary_dir()
    assert path.exists()  # 被刪掉時自動補回


def test_trash_missing_file(tmp_path: Path) -> None:
    from meeting_scribe.storage.trash import move_to_recycle_bin

    with pytest.raises(FileNotFoundError):
        move_to_recycle_bin(tmp_path / "nope.csv")


@pytest.mark.parametrize("status,ok,text", [(200, True, "有效"), (401, False, "金鑰無效"), (404, False, "HTTP 404")])
def test_check_key(monkeypatch: pytest.MonkeyPatch, status: int, ok: bool, text: str) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> httpx.Response:
        calls.append(url)
        return httpx.Response(status, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    result_ok, msg = gui_settings.check_key("azure_speech_key", "k", {"stt": {"azure": {"region": "eastasia"}}})
    assert result_ok is ok and text in msg
    assert calls[0].startswith("https://eastasia.api.cognitive.microsoft.com/")


def test_check_key_needs_region() -> None:
    ok, msg = gui_settings.check_key("azure_speech_key", "k", {"stt": {"azure": {}}})
    assert not ok and "區域" in msg
