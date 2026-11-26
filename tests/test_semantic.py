"""AI 校正（語意與講者角色）：建議、驗證、續跑、確認後套用、可復原。全部用模擬的 AI 回應，不花費用。"""

import os
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from meeting_scribe import tasks
from meeting_scribe.config import load_config
from meeting_scribe.llm.base import LLMClient, LLMError
from meeting_scribe.models import RawSegment
from meeting_scribe.postprocess.glossary import GlossaryEntry
from meeting_scribe.postprocess.semantic import (
    MixedSegment,
    RoleSuggestion,
    SegmentReassign,
    SpeakerMerge,
    Suggestions,
    TextFix,
    acceptable_fix,
    apply_speaker_changes,
    apply_suggestions,
    glossary_fix,
    suggest_fixes,
    suggest_reassignments,
    suggest_roles,
)
from meeting_scribe.transcript_io import TranscriptDocument

TEXTS = [("講者1", "好，我們開始開會，今天討論獨架防制。"), ("講者2", "公路據報告，修法預計11月完成。"),
         ("講者1", "好，請監理司補充。"), ("講者3", "監理司這邊沒有意見。")]


def _lines() -> list[tuple[int, str, str, str]]:
    return [(i, f"00:00:{i * 10:02d}", spk, text) for i, (spk, text) in enumerate(TEXTS)]


class FakeLLM(LLMClient):
    name = "fake"

    def __init__(self, replies: list[Any]) -> None:
        super().__init__()
        self.models = {"correct": "fake-sonnet"}
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def complete_json(self, system: str, user: str, schema: dict, task: str, max_tokens: int = 16000) -> dict:
        self.calls.append({"system": system, "user": user, "task": task})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


GLOSSARY = [GlossaryEntry(term="公路局", misspellings=["公路據"], category="機關")]


# ---- 規則與驗證 ----
def test_glossary_fix_and_validation() -> None:
    assert glossary_fix("公路據報告", GLOSSARY) == ("公路局報告", ["公路據→公路局"])
    assert acceptable_fix("今天討論獨架防制。", "今天討論毒駕防制。") is None
    assert acceptable_fix("修法預計11月完成。", "修法預計12月完成。") == "數字被改動"
    assert "幅度太大" in (acceptable_fix("好，請監理司補充。", "主席請公共運輸及監理司針對本案詳細說明。") or "")
    assert acceptable_fix("好", "好") == "沒有修改"


# ---- 文字校正 ----
def test_suggest_fixes_combines_glossary_and_ai_and_resumes(tmp_path: Path) -> None:
    reply = {"items": [
        {"id": "S0001", "text": "好，我們開始開會，今天討論毒駕防制。", "reason": "獨架→毒駕：前後文談毒品駕駛"},
        {"id": "S0003", "text": "主席請公共運輸及監理司詳細補充本案。", "reason": "改寫"},  # 幅度太大 → 擋下
        {"id": "S9999", "text": "不存在", "reason": "x"},  # 不存在的編號 → 略過
    ]}
    llm = FakeLLM([reply])
    ckpt = tmp_path / "semantic.json"
    out = suggest_fixes(_lines(), {"title": "道安研商會議"}, ["講者1", "講者2", "講者3"], GLOSSARY, "【詞庫】",
                        llm, 3000, None, ckpt)
    fixes = {f.index: f for f in out.fixes}
    assert fixes[0].corrected.endswith("毒駕防制。") and fixes[0].source == "AI"
    assert fixes[1].corrected == "公路局報告，修法預計11月完成。" and fixes[1].source == "詞庫"
    assert "公路據→公路局" in fixes[1].reason
    assert 2 not in fixes and out.rejected == 1
    assert llm.calls[0]["task"] == "correct" and "公路局報告" in llm.calls[0]["user"]  # AI 看到的是詞庫修正後的文字
    assert "道安研商會議" in llm.calls[0]["user"]
    again = suggest_fixes(_lines(), {"title": "道安研商會議"}, ["講者1", "講者2", "講者3"], GLOSSARY, "【詞庫】",
                          FakeLLM([]), 3000, None, ckpt)
    assert again.reused == 1 and len(again.fixes) == 2  # 內容沒變 → 沿用，不重複計費


def test_suggest_fixes_batches_with_context_and_fatal_error(tmp_path: Path) -> None:
    llm = FakeLLM([{"items": []}, LLMError("額度不足", fatal=True)])
    with pytest.raises(LLMError):
        suggest_fixes(_lines(), {}, [], [], "", llm, 30, None, tmp_path / "s.json")
    assert "前文（只供參考" in llm.calls[1]["user"]
    resumed = FakeLLM([{"items": []}, {"items": []}, {"items": []}])
    out = suggest_fixes(_lines(), {}, [], [], "", resumed, 30, None, tmp_path / "s.json")
    assert out.reused == 1 and len(resumed.calls) == 2  # 第 1 批沿用，從中斷處繼續


# ---- 講者角色 ----
def test_suggest_roles() -> None:
    llm = FakeLLM([{"speakers": [
        {"speaker": "講者1", "name": "主席", "confidence": "高", "evidence": ["S0001", "S0003"], "reason": "主持會議"},
        {"speaker": "講者2", "name": "公路局", "confidence": "中", "evidence": [], "reason": "自稱公路局"},
        {"speaker": "講者9", "name": "不存在", "confidence": "高", "evidence": ["S0001"], "reason": "x"},
        {"speaker": "講者3", "name": "無法判斷", "confidence": "低", "evidence": [], "reason": ""},
    ]}])
    out = suggest_roles(_lines(), {}, ["講者1", "講者2", "講者3"], set(), llm)
    roles = {r.speaker: r for r in out.roles}
    assert roles["講者1"].name == "主席" and roles["講者1"].evidence == [0, 2]
    assert roles["講者2"].confidence == "低"  # 沒有依據片段 → 降為低
    assert set(roles) == {"講者1", "講者2"}
    assert FakeLLM([]).calls == [] and suggest_roles(_lines(), {}, ["講者1"], {"講者1"}, FakeLLM([])).roles == []


def test_apply_skips_segments_changed_meanwhile(tmp_path: Path) -> None:
    doc = TranscriptDocument(source="t", folder=str(tmp_path),
                             segments=[RawSegment(start=i, end=i + 1, speaker=s, text=t) for i, (s, t) in enumerate(TEXTS)])
    doc.segments[1] = doc.segments[1].model_copy(update={"text": "使用者自己改過"})
    fixes = [TextFix(0, TEXTS[0][1], "好，我們開始開會，今天討論毒駕防制。", "", "AI"),
             TextFix(1, TEXTS[1][1], "公路局報告，修法預計11月完成。", "", "詞庫")]
    applied, named = apply_suggestions(doc, fixes, [RoleSuggestion("講者1", "主席", "高", [0], "")])
    assert (applied, named) == (1, 1)
    assert doc.segments[1].text == "使用者自己改過" and doc.display_name("講者1") == "主席"


def test_suggest_corrections_without_key_uses_glossary_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "default_glossaries", list)
    monkeypatch.setattr(tasks, "load_glossaries", lambda paths: GLOSSARY)
    doc = TranscriptDocument(source="t", folder=str(tmp_path),
                             segments=[RawSegment(start=i, end=i + 1, speaker=s, text=t) for i, (s, t) in enumerate(TEXTS)])
    out = tasks.suggest_corrections(doc, load_config())
    assert [f.corrected for f in out.fixes] == ["公路局報告，修法預計11月完成。"] and not out.roles
    assert any("未設定" in n for n in out.notes)


def test_suggest_corrections_with_ai(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeLLM([{"items": [{"id": "S0001", "text": "好，我們開始開會，今天討論毒駕防制。", "reason": "同音"}]},
                   {"speakers": [{"speaker": "講者1", "name": "主席", "confidence": "高", "evidence": ["S0001"],
                                  "reason": "主持"}], "merges": []},
                   {"reassign": [{"id": "S0002", "speaker": "講者3", "confidence": "中", "reason": "回應點名"}],
                    "mixed": []}])
    monkeypatch.setattr(tasks, "get_secret", lambda n: "sk-test")
    monkeypatch.setattr("meeting_scribe.llm.create_llm", lambda cfg: llm)
    doc = TranscriptDocument(source="t", folder=str(tmp_path),
                             segments=[RawSegment(start=i, end=i + 1, speaker=s, text=t) for i, (s, t) in enumerate(TEXTS)])
    out = tasks.suggest_corrections(doc, load_config())
    assert [f.index for f in out.fixes] == [0] and out.roles[0].name == "主席"
    assert [(r.index, r.suggested) for r in out.reassigns] == [(1, "講者3")]
    assert [c["task"] for c in llm.calls] == ["correct", "correct", "correct"]
    for name in ("semantic.json", "roles.json", "speaker_check.json"):
        assert (tmp_path / "_work" / name).exists()  # 每一步都存檔，可續跑


# ---- 介面 ----
@pytest.fixture
def page(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    from PySide6.QtWidgets import QApplication, QMessageBox

    from meeting_scribe.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    w = MainWindow(secret_store={"get_secret": lambda n: None, "set_secret": lambda n, v: None,
                                 "delete_secret": lambda n: None})
    doc = TranscriptDocument(source="t", folder=str(tmp_path / "job"),
                             segments=[RawSegment(start=i * 10, end=i * 10 + 5, speaker=s, text=t)
                                       for i, (s, t) in enumerate(TEXTS)])
    w.document.load(doc)
    monkeypatch.setattr(w.document, "_choose_correction_source", lambda: ("cloud", None, None))  # 選「雲端比對」
    yield w.document
    w.document._dirty = False
    w.close()
    del app


def _wait(page: Any) -> None:
    from PySide6.QtTest import QTest

    for _ in range(200):
        QTest.qWait(10)
        if not page.panel.running:
            return


def _suggestions() -> Suggestions:
    return Suggestions(
        fixes=[TextFix(0, TEXTS[0][1], "好，我們開始開會，今天討論毒駕防制。", "獨架→毒駕", "AI"),
               TextFix(1, TEXTS[1][1], "公路局報告，修法預計11月完成。", "詞庫：公路據→公路局", "詞庫")],
        roles=[RoleSuggestion("講者1", "主席", "高", [0], "主持會議"),
               RoleSuggestion("講者3", "監理司", "低", [3], "猜測")])


def test_dialog_defaults_and_edits(page: Any) -> None:
    from PySide6.QtCore import Qt

    from meeting_scribe.gui.correction_dialog import CorrectionDialog

    d = CorrectionDialog(page, page.doc, _suggestions())
    assert d.role_table.item(1, 0).checkState() == Qt.CheckState.Unchecked  # 低把握度預設不勾
    d.fix_table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)  # 不要第 2 筆
    d.fix_table.item(0, 4).setText("好，我們開始開會，今天討論毒駕防制案。")  # 修改建議內容
    fixes, roles = d.chosen()
    assert [f.corrected for f in fixes] == ["好，我們開始開會，今天討論毒駕防制案。"]
    assert [(r.speaker, r.name) for r in roles] == [("講者1", "主席")]


def test_ai_correct_runs_then_asks_then_applies_and_undo(page: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_suggest(doc: Any, cfg: Any, progress: Any = None, cancel: Any = None) -> Suggestions:
        calls.append("run")
        return _suggestions()

    shown: list[Suggestions] = []
    monkeypatch.setattr(tasks, "suggest_corrections", fake_suggest)

    def choose(s: Suggestions) -> Any:
        shown.append(s)  # 整份跑完後才顯示確認視窗
        return s.fixes, [s.roles[0]], [], []

    monkeypatch.setattr(page, "_open_correction_dialog", choose)
    page.ai_correct()
    _wait(page)
    assert calls == ["run"] and len(shown) == 1
    assert page.doc.segments[0].text.endswith("毒駕防制。") and page.doc.segments[1].text.startswith("公路局報告")
    assert page.table.item(0, 1).text() == "主席"
    assert "AI 校正（2 處文字、1 位講者）" in page.undo_button.toolTip()
    page.undo()  # 一次復原整個 AI 校正
    assert page.doc.segments[0].text == TEXTS[0][1] and page.table.item(0, 1).text() == "講者1"


def test_ai_correct_cancel_changes_nothing(page: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "suggest_corrections", lambda *a, **k: _suggestions())
    monkeypatch.setattr(page, "_open_correction_dialog", lambda s: None)  # 按「取消」
    page.ai_correct()
    _wait(page)
    assert page.doc.segments[0].text == TEXTS[0][1] and not page.undo_button.isEnabled()


# ---- 講者檢查 ----
def test_roles_call_also_returns_merges() -> None:
    llm = FakeLLM([{"speakers": [], "merges": [
        {"source": "講者3", "target": "講者2", "confidence": "高", "evidence": ["S0004"], "reason": "都自稱監理司"},
        {"source": "講者3", "target": "講者1", "confidence": "高", "evidence": ["S0004"], "reason": "重複的來源"},
        {"source": "講者9", "target": "講者1", "confidence": "高", "evidence": [], "reason": "不存在"},
        {"source": "講者1", "target": "講者1", "confidence": "高", "evidence": [], "reason": "自己"},
    ]}])
    out = suggest_roles(_lines(), {}, ["講者1", "講者2", "講者3"], {"講者1", "講者2", "講者3"}, llm)
    assert [(m.source, m.target, m.evidence) for m in out.merges] == [("講者3", "講者2", [3])]
    assert "merges" in llm.calls[0]["user"]  # 都已命名時仍檢查是否要合併


def test_suggest_reassignments_validates_and_resumes(tmp_path: Path) -> None:
    reply = {"reassign": [
        {"id": "S0002", "speaker": "講者3", "confidence": "高", "reason": "被點名後回應"},
        {"id": "S0002", "speaker": "講者2", "confidence": "高", "reason": "和目前相同 → 略過"},
        {"id": "S0003", "speaker": "講者9", "confidence": "高", "reason": "不存在的講者 → 略過"},
    ], "mixed": [{"id": "S0004", "reason": "同一段裡有提問又有回答"}, {"id": "S9999", "reason": "x"}]}
    llm = FakeLLM([reply])
    ckpt = tmp_path / "check.json"
    out = suggest_reassignments(_lines(), {"title": "研商會"}, ["講者1", "講者2", "講者3"], {"講者1": "主席"},
                                llm, 12000, None, ckpt)
    assert [(r.index, r.current, r.suggested) for r in out.reassigns] == [(1, "講者2", "講者3")]
    assert [(m.index, m.reason) for m in out.mixed] == [(3, "同一段裡有提問又有回答")]
    assert "講者1（主席）" in llm.calls[0]["user"]
    again = suggest_reassignments(_lines(), {"title": "研商會"}, ["講者1", "講者2", "講者3"], {"講者1": "主席"},
                                  FakeLLM([]), 12000, None, ckpt)
    assert again.reused == 1 and len(again.reassigns) == 1  # 沿用，不重複計費
    assert suggest_reassignments(_lines(), {}, ["講者1"], {}, FakeLLM([])).reassigns == []  # 只有一位講者不檢查


def test_apply_speaker_changes(tmp_path: Path) -> None:
    doc = TranscriptDocument(source="t", folder=str(tmp_path),
                             segments=[RawSegment(start=i, end=i + 1, speaker=s, text=t) for i, (s, t) in enumerate(TEXTS)])
    doc.segments[2] = doc.segments[2].model_copy(update={"speaker": "講者3"})  # 之後手動改過
    merged, moved = apply_speaker_changes(
        doc, [SpeakerMerge("講者3", "講者2", "高", [3], "")],
        [SegmentReassign(0, "講者1", "講者2", "高", ""), SegmentReassign(2, "講者1", "講者2", "高", "")])
    assert (merged, moved) == (1, 1)
    assert [s.speaker for s in doc.segments] == ["講者2", "講者2", "講者2", "講者2"]


def test_dialog_speaker_check_tab(page: Any) -> None:
    from PySide6.QtCore import Qt

    from meeting_scribe.gui.correction_dialog import CorrectionDialog

    s = Suggestions(merges=[SpeakerMerge("講者3", "講者2", "低", [], "猜測")],
                    reassigns=[SegmentReassign(1, "講者2", "講者3", "高", "被點名後回應")],
                    mixed=[MixedSegment(3, "有提問又有回答")])
    d = CorrectionDialog(page, page.doc, s)
    assert d.tabs.currentIndex() == 2 and "講者檢查（3）" in d.tabs.tabText(2)
    assert d.check_table.item(0, 0).checkState() == Qt.CheckState.Unchecked  # 低把握度預設不勾
    assert not d.check_table.item(2, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable  # 只提醒，不能勾
    d._set_all(d.check_table, Qt.CheckState.Checked)
    merges, reassigns = d.chosen_speaker_checks()
    assert len(merges) == 1 and [r.index for r in reassigns] == [1]


def test_ai_correct_applies_speaker_checks_and_undo(page: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    s = Suggestions(reassigns=[SegmentReassign(1, "講者2", "講者3", "高", "被點名後回應")],
                    merges=[SpeakerMerge("講者3", "講者1", "中", [3], "同一人")])
    monkeypatch.setattr(tasks, "suggest_corrections", lambda *a, **k: s)
    monkeypatch.setattr(page, "_open_correction_dialog", lambda x: ([], [], [], x.reassigns))  # 只套用改講者
    page.ai_correct()
    _wait(page)
    assert [x.speaker for x in page.doc.segments] == ["講者1", "講者3", "講者1", "講者3"]
    assert "講者檢查 1 項" in page.undo_button.toolTip()
    page.undo()
    assert [x.speaker for x in page.doc.segments] == ["講者1", "講者2", "講者1", "講者3"]