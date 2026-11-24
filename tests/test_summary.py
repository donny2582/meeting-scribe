"""第 2 階段：AI 整理討論事項、結論、待辦（假的 LLM，不呼叫真實 API）。"""

import json
from pathlib import Path
from typing import Any

import pytest

from meeting_scribe import tasks
from meeting_scribe.config import load_config
from meeting_scribe.llm.base import LLMClient, LLMError
from meeting_scribe.models import RawSegment, Summary
from meeting_scribe.render.minutes_text import cn_number
from meeting_scribe.summarize.schema import summary_schema
from meeting_scribe.summarize.summarizer import Line, SummaryError, call_summary, make_line, summarize
from meeting_scribe.summarize.validator import validate_summary
from meeting_scribe.transcript_io import TranscriptDocument


def _all_objects(schema: dict) -> list[dict]:
    found = []
    if schema.get("type") == "object":
        found.append(schema)
        for v in schema["properties"].values():
            found += _all_objects(v)
    if schema.get("type") == "array":
        found += _all_objects(schema["items"])
    return found


def test_schema_is_strict_and_limited_depth() -> None:
    objs = _all_objects(summary_schema())
    assert all(o["additionalProperties"] is False and set(o["required"]) == set(o["properties"]) for o in objs)
    item1 = summary_schema()["properties"]["conclusions"]["items"]["properties"]["items"]["items"]
    item3 = item1["properties"]["children"]["items"]["properties"]["children"]["items"]
    assert "children" not in item3["properties"]  # 最多 3 層


def test_cn_number() -> None:
    assert [cn_number(n) for n in (1, 9, 10, 11, 20, 25)] == ["一", "九", "十", "十一", "二十", "二十五"]


def _summary(**kw: Any) -> dict:
    base = {
        "meeting_type": "agenda",
        "discussion_items": [{"title": "永續提升人行安全計畫", "children": [], "evidence": ["S0001"]}],
        "conclusions": [{"title": "永續提升人行安全計畫", "items": [
            {"role": "單位意見", "speaker": "公路局", "text": "公路局：配合修正績效指標。", "evidence": ["S0002", "S9999"],
             "children": []},
            {"role": "主席裁示", "speaker": "主席", "text": "主席裁示：請公路局標明績效指標。", "evidence": ["S0003"],
             "children": []},
        ]}, {"title": "後續時程", "items": [
            {"role": "主席裁示", "speaker": "主席", "text": "主席裁示：（未明確裁示）", "evidence": ["S0003"], "children": []},
            {"role": "一般敘述", "speaker": "講者9", "text": "沒有依據的內容", "evidence": ["S8888"], "children": []},
        ]}],
        "action_items": [{"task": "提送修正版計畫", "owner": "公路局", "due": "115年11月11日", "evidence": ["S0003"]}],
        "mindmap": {"text": "研商會", "children": [{"text": "人行安全", "children": []}]},
        "warnings": ["講者2 的身分不確定"],
    }
    base.update(kw)
    return base


def test_validator_marks_pending() -> None:
    s = Summary.model_validate(_summary())
    rep = validate_summary(s, {"S0001", "S0002", "S0003"})
    items = [i for sec in s.conclusions for i in sec.items]
    assert items[0].evidence == ["S0002"] and not items[0].pending  # 不存在的編號被移除
    assert items[2].pending and "未明確裁示" in items[2].pending_reason
    assert items[3].pending and items[3].evidence == []
    assert rep.pending == 2 and rep.removed_ids == 2


def test_validator_uncertain_speaker() -> None:
    s = Summary.model_validate(_summary())
    validate_summary(s, {"S0001", "S0002", "S0003"}, uncertain_ids={"S0003"})
    assert s.conclusions[0].items[1].pending and "講者" in s.conclusions[0].items[1].pending_reason


class FakeLLM(LLMClient):
    name = "fake"

    def __init__(self, replies: list[Any]) -> None:
        super().__init__()
        self.models = {"summary": "fake-sonnet"}
        self.replies = replies
        self.calls: list[dict] = []

    def complete_json(self, system: str, user: str, schema: dict, task: str, max_tokens: int = 16000) -> dict:
        assert task == "summary"
        self.calls.append({"system": system, "user": user})
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_call_summary_retries_then_fails() -> None:
    llm = FakeLLM([{"meeting_type": "topic"}, {"oops": 1}, _summary()])
    assert call_summary(llm, "s", "u", 1000).meeting_type == "agenda"
    assert "上一次的輸出有錯誤" in llm.calls[1]["user"]
    with pytest.raises(SummaryError, match="已重試 2 次"):
        call_summary(FakeLLM([{"conclusions": "bad"}]), "s", "u", 1000)
    with pytest.raises(LLMError):
        call_summary(FakeLLM([LLMError("金鑰無效", fatal=True)]), "s", "u", 1000)


def _lines(n: int, gap: float = 30) -> list[Line]:
    return [make_line(f"S{i + 1:04d}", i * gap, f"講者{i % 3 + 1}", f"第{i}段內容。") for i in range(n)]


def test_summarize_single_pass_and_cache(tmp_path: Path) -> None:
    info = {"title": "研商會", "meeting_type": "agenda", "agenda": ["永續提升人行安全計畫"], "chair": "陳次長"}
    llm = FakeLLM([_summary()])
    s = summarize(_lines(5), info, ["講者1", "講者2"], llm, load_config(), "【用語對照表】", None, tmp_path)
    assert s.model == "fake-sonnet" and len(llm.calls) == 1
    system, user = llm.calls[0]["system"], llm.calls[0]["user"]
    assert "議程逐項式" in system and "共通規則" in system and "【用語對照表】" in system
    assert "S0003 [00:01:00] 講者3：第2段內容。" in user and "- 永續提升人行安全計畫" in user and "陳次長" in user
    summarize(_lines(5), info, ["講者1", "講者2"], llm, load_config(), "【用語對照表】", None, tmp_path)
    assert len(llm.calls) == 1  # 內容沒變 → 沿用，不重複計費
    info2 = dict(info, meeting_type="qa")
    summarize(_lines(5), info2, ["講者1"], llm, load_config(), "", None, tmp_path)
    assert len(llm.calls) == 2  # 換會議類型 → 重新整理


def test_summarize_long_meeting_in_parts_and_resume(tmp_path: Path) -> None:
    cfg = load_config()
    cfg["llm"]["max_chars_single_pass"] = 50  # 強制分段
    lines = _lines(90, gap=30)  # 45 分鐘 → 3 段（每 20 分鐘）
    info = {"title": "工作會議", "meeting_type": "topic"}
    failing = FakeLLM([_summary(), _summary(), LLMError("額度不足", fatal=True)])
    with pytest.raises(LLMError):
        summarize(lines, info, [], failing, cfg, "", None, tmp_path)
    assert len(list(tmp_path.glob("summary_topic_part*.json"))) == 2
    llm = FakeLLM([_summary()])
    messages: list[str] = []
    summarize(lines, info, [], llm, cfg, "", lambda p, m: messages.append(m), tmp_path)
    assert len(llm.calls) == 2  # 只補做第 3 段＋合併
    assert "第 3/3 段" in llm.calls[0]["user"] and "分段合併" in llm.calls[1]["user"]
    assert sum("沿用已完成" in m for m in messages) == 2


def test_summarize_meeting_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    segs = [RawSegment(start=600 + i * 10, end=605 + i * 10, speaker=f"講者{i + 1}", text=f"內容{i}") for i in range(4)]
    doc = TranscriptDocument(source="t", folder=str(tmp_path), segments=segs)
    doc.rename_speaker("講者2", "公路局")
    doc.info.title, doc.info.date, doc.info.meeting_type = "道安研商會議", "2026-11-09", "agenda"
    doc.info.start_time, doc.info.end_time, doc.info.want_action_table = "14:00", "16:30", True
    monkeypatch.setattr(tasks, "get_secret", lambda n: "sk-ant-test")
    llm = FakeLLM([_summary()])
    monkeypatch.setattr("meeting_scribe.llm.create_llm", lambda cfg: llm)
    pairs = [(0, segs[0]), (1, segs[1]), (2, segs[2])]  # 第 4 段被當成贅字移除，編號仍對應原始位置
    out = tasks.summarize_meeting(doc, pairs, load_config(), tmp_path / "輸出")
    assert "S0002 [00:10:10] 公路局：內容1" in llm.calls[0]["user"]
    import docx

    assert out.minutes_path.name == "會議紀錄_道安研商會議_1151109.docx"
    d = docx.Document(str(out.minutes_path))
    texts = [p.text for p in d.paragraphs]
    for expected in ("道安研商會議", "壹、會議時間：115年11月9日(星期一)下午2時00分", "伍、討論事項：",
                     "一、永續提升人行安全計畫", "陸、結論：", "(一)公路局：配合修正績效指標。",
                     "(一)主席裁示：（未明確裁示）（待查證）", "待辦事項追蹤表", "散會(下午4時30分)"):
        assert expected in texts, expected
    comments = [c.text for c in d.comments]
    assert any(c == "依據：00:10:10–00:10:15 公路局" for c in comments)
    assert any("待查證：主席未明確裁示" in c for c in comments)
    assert any(c.startswith("AI 提醒的不確定之處") and "講者2 的身分不確定" in c for c in comments)
    row = [c.text for c in d.tables[0].rows[1].cells]
    assert row == ["1", "提送修正版計畫", "公路局", "115年11月11日", "00:10:20"]
    assert out.png_path is None  # 沒勾心智圖
    assert json.loads(out.json_path.read_text(encoding="utf-8"))["meeting_type"] == "agenda"
    assert out.validation.pending == 2


def test_summarize_meeting_requires_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = TranscriptDocument(source="t", folder=str(tmp_path),
                             segments=[RawSegment(start=0, end=1, speaker="講者1", text="好")])
    monkeypatch.setattr(tasks, "get_secret", lambda n: None)
    with pytest.raises(SummaryError, match="需要 AI"):
        tasks.summarize_meeting(doc, list(enumerate(doc.segments)), load_config(), tmp_path)
