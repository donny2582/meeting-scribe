"""嚴格檢查發現的問題（重現測試）：復原記憶體、再次 AI 校正改到已命名講者、載入校正檔的時間碼、清空講者儲存格。"""

import os
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from meeting_scribe.models import RawSegment
from meeting_scribe.postprocess import ai_review
from meeting_scribe.postprocess.semantic import suggest_roles
from meeting_scribe.transcript_io import EditHistory, TranscriptDocument


def _segs(n: int) -> list[RawSegment]:
    return [RawSegment(start=i * 5, end=i * 5 + 4, speaker=f"講者{i % 3 + 1}", text="會議發言內容" * 10)
            for i in range(n)]


# B6：復原紀錄不可整份複製比對基準（2 小時會議 200 步約 174 MB）
def test_history_does_not_deep_copy_baseline() -> None:
    doc = TranscriptDocument(source="t", segments=_segs(1500))
    ai_review.start_review(doc, list(doc.segments), "雲端比對")
    h = EditHistory()
    tracemalloc.start()
    for i in range(200):
        h.record(doc, f"第{i}步")
    used, _peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert used < 30 * 1024 * 1024, f"復原紀錄用了 {used / 1048576:.0f} MB"
    ai_review.set_restored(doc, 0, True)  # 修改後復原，比對狀態要正確回到之前
    doc.ai_review.restored.append(5)  # type: ignore[union-attr]
    h.undo(doc)
    assert doc.ai_review is not None and doc.ai_review.restored == []


# B7：已經逐列命名的講者（例：主席）再次 AI 校正時，不可再被建議改名
def test_second_ai_run_keeps_named_speakers() -> None:
    from tests.test_semantic import FakeLLM

    lines = [(0, "00:00:00", "主席", "好，開始開會。"), (1, "00:00:05", "講者2", "公路局報告。")]
    llm = FakeLLM([{"speakers": [{"speaker": "主席", "name": "公路局", "confidence": "高", "evidence": ["S0001"],
                                  "reason": "x"},
                                 {"speaker": "講者2", "name": "公路局", "confidence": "高", "evidence": ["S0002"],
                                  "reason": "自稱"}], "merges": []}])
    out = suggest_roles(lines, {}, ["主席", "講者2"], set(), llm)
    assert [(r.speaker, r.name) for r in out.roles] == [("講者2", "公路局")]
    assert "主席" not in llm.calls[0]["user"].split("請推測這些講者的身分：")[1].splitlines()[0]


# B16：載入校正檔時，時間碼以逐字稿草稿為準
def test_load_files_keeps_draft_timecodes(tmp_path: Path) -> None:
    (tmp_path / "d.txt").write_text("[00:00:05] 講者1：好。\n[00:00:20] 講者2：報告。\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("[00:00:05] 主席：好。\n[00:00:20] 公路局：報告。\n", encoding="utf-8")
    result = ai_review.load_files(tmp_path / "d.txt", tmp_path / "c.txt", tmp_path / "w")
    assert result.timed is True and len(result.joined) == 2


# ---- 介面 ----
@pytest.fixture
def page(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    from PySide6.QtWidgets import QApplication, QMessageBox

    from meeting_scribe.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    w = MainWindow(secret_store={"get_secret": lambda n: None, "set_secret": lambda n, v: None,
                                 "delete_secret": lambda n: None})
    doc = TranscriptDocument(source="t", folder=str(tmp_path / "job"), has_timecodes=False, segments=_segs(3))
    w.document.load(doc)
    w.document.monkeypatch = monkeypatch  # type: ignore[attr-defined]
    yield w.document
    w.document._dirty = False
    w.close()
    del app


# B1：清空講者儲存格時，資料沒變，畫面也要回到原本的講者
def test_clearing_speaker_cell_restores_display(page: Any) -> None:
    page.table.item(0, 1).setText("")
    assert page.doc.segments[0].speaker == "講者1"
    assert page.table.item(0, 1).text() == "講者1"


def test_files_correction_shows_draft_timecodes(page: Any, tmp_path: Path) -> None:
    (tmp_path / "d.txt").write_text("[00:00:05] 講者1：好。\n[00:00:20] 講者2：報告。\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("[00:00:05] 主席：好。\n[00:00:20] 公路局：報告。\n", encoding="utf-8")
    page.monkeypatch.setattr(page, "_choose_correction_source", lambda: ("files", tmp_path / "d.txt",
                                                                         tmp_path / "c.txt"))
    page.ai_correct()
    assert page.table.item(0, 0).text() == "00:00:05"  # 原本的文件沒有時間碼，改用草稿的時間碼
    page.undo()
    assert page.table.item(0, 0).text() == "—"  # 復原後回到原本的狀態


# ---- 除錯模式 ----
@pytest.fixture
def _restore_logging() -> Any:
    import logging

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for h in root.handlers:
        if h not in handlers:
            h.close()
    root.handlers[:] = handlers
    root.setLevel(level)


def test_debug_mode_logs_details_without_transcript(_restore_logging: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import logging

    import httpx

    from meeting_scribe import diagnostics
    from meeting_scribe.config import load_config
    from meeting_scribe.llm.openai_llm import OpenAILLM

    secret = "主席裁示毒駕防制修法今年底完成"  # 逐字稿內容：不可以出現在日誌
    path = diagnostics.setup_logging(verbose=False, console=False)
    diagnostics.apply_debug(True)
    assert logging.getLogger().isEnabledFor(logging.DEBUG)

    def post(url: str, json: dict, headers: dict, timeout: Any) -> httpx.Response:
        body = {"model": "gpt-6.2-sol", "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "choices": [{"finish_reason": "stop", "message": {"content": _dumps({"items": []})}}]}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    def _dumps(o: Any) -> str:
        return json.dumps(o)

    monkeypatch.setattr(httpx, "post", post)
    cfg = load_config()
    cfg["llm"]["max_retries"] = 0
    OpenAILLM(cfg, api_key="sk-test").complete_json("系統提示", "逐字稿：" + secret, {}, "correct")
    for h in logging.getLogger().handlers:
        h.flush()
    text = path.read_text(encoding="utf-8")
    assert "除錯模式：開啟" in text and "OpenAI 呼叫開始：工作=correct" in text and "OpenAI 回應：HTTP 200" in text
    assert secret not in text and "sk-test" not in text  # 不記錄逐字稿內容與金鑰
    diagnostics.apply_debug(False)
    assert not logging.getLogger().isEnabledFor(logging.DEBUG)


def test_debug_toggle_in_version_tab(_restore_logging: Any) -> None:
    import logging

    from PySide6.QtWidgets import QApplication

    from meeting_scribe import diagnostics
    from meeting_scribe.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    diagnostics.setup_logging(console=False)
    w = MainWindow(secret_store={"get_secret": lambda n: None, "set_secret": lambda n, v: None,
                                 "delete_secret": lambda n: None})
    assert not w.version.debug.isChecked()
    w.version.debug.setChecked(True)
    assert logging.getLogger().isEnabledFor(logging.DEBUG) and w.cfg["general"]["debug"] is True
    assert "除錯模式：開啟" in "\n".join(diagnostics.system_info())
    w.version.debug.setChecked(False)
    assert w.cfg["general"]["debug"] is False
    w.close()
    del app


def test_debug_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    from meeting_scribe import diagnostics

    assert not diagnostics.debug_enabled({})
    monkeypatch.setenv("MEETINGSCRIBE_DEBUG", "1")
    assert diagnostics.debug_enabled({})