"""刪除贅字：規則層＋AI 層＋子序列驗證＋逐批存檔（假的 LLM，不呼叫真實 API）。"""

import json
from pathlib import Path
from typing import Any

import pytest

from meeting_scribe import tasks
from meeting_scribe.config import load_config
from meeting_scribe.llm.base import LLMClient, LLMError
from meeting_scribe.models import RawSegment
from meeting_scribe.postprocess.filler import clean_texts, remove_fillers_rule
from meeting_scribe.postprocess.lang_detect import LanguageRegistry

ZH = ["呃", "嗯", "欸", "啊", "那個", "就是說", "然後然後"]


def test_rule_keeps_meaningful_nage() -> None:
    assert remove_fillers_rule("那個方案要再討論", ZH, ["對"]) == "那個方案要再討論"
    assert remove_fillers_rule("呃，那個，我們開始", ZH, ["對"]) == "我們開始"
    assert remove_fillers_rule("對對對，就是說，好", ZH, ["對"]) == "對，好"


class FakeLLM(LLMClient):
    name = "fake"

    def __init__(self, transform: Any, fail_on: int | None = None) -> None:
        super().__init__()
        self.models = {"filler": "fake-haiku"}
        self.transform = transform
        self.fail_on = fail_on
        self.calls: list[str] = []

    def complete_json(self, system: str, user: str, schema: dict, task: str, max_tokens: int = 16000) -> dict:
        assert task == "filler"
        self.calls.append(user)
        if len(self.calls) == self.fail_on:
            raise LLMError("Anthropic 請求過多或額度不足，請稍後再試", fatal=True)
        items = json.loads(user.split("\n", 1)[1])["items"]
        return {"items": [{"id": i["id"], "text": self.transform(i["text"])} for i in items]}


def rule(_sid: str, text: str) -> str:
    return remove_fillers_rule(text, ZH, ["對"])


TEXTS = [
    ("S1", "我我我們那個要再再大家一起討論，嗯。"),
    ("S2", "嗯。"),
    ("S3", "這個這個方案下週提交。"),
]


def test_ai_result_used_when_only_deletions() -> None:
    fixes = {"我我我們那個要再再大家一起討論，嗯。": "我們要再大家一起討論。", "嗯。": "",
             "這個這個方案下週提交。": "這個方案下週提交。"}
    out, rep = clean_texts(TEXTS, rule, FakeLLM(lambda t: fixes[t]))
    assert out == {"S1": "我們要再大家一起討論。", "S2": "", "S3": "這個方案下週提交。"}
    assert rep.ai_used and rep.ai_batches == 1 and rep.ai_rejected == 0 and rep.dropped == 1


def test_ai_rewrite_rejected_falls_back_to_rule() -> None:
    # AI 擅自改寫（加字、換字）→ 不通過子序列驗證，改用規則層結果
    out, rep = clean_texts(TEXTS, rule, FakeLLM(lambda t: t.replace("提交", "繳交").replace("討論", "研議並討論")))
    assert out["S3"] == rule("S3", "這個這個方案下週提交。")
    assert "研議" not in out["S1"]
    assert rep.ai_rejected == 2


def test_ai_cannot_delete_most_of_long_segment() -> None:
    long = "我們今天要討論道路交通安全推動計畫的第三章內容與執行時程"
    out, rep = clean_texts([("S1", long)], rule, FakeLLM(lambda t: "我們"))
    assert out["S1"] == long and rep.ai_rejected == 1


def test_without_llm_rule_only() -> None:
    out, rep = clean_texts(TEXTS, rule, None)
    assert not rep.ai_used and out["S2"] == ""  # 「嗯。」整段刪除


def test_fatal_error_stops_and_resume(tmp_path: Path) -> None:
    ckpt = tmp_path / "filler.json"
    texts = [(f"S{i}", f"嗯，第{i}段內容那個，好。") for i in range(3)]

    def ai(t: str) -> str:
        return t.replace("嗯，", "").replace("那個，", "")

    first = FakeLLM(ai, fail_on=2)
    out, rep = clean_texts(texts, rule, first, max_chars=10, checkpoint_file=ckpt)  # 每批 1 段
    assert rep.ai_error and rep.ai_batches == 1
    assert out["S2"] == rule("S2", texts[2][1])  # 中斷後其餘用規則層

    second = FakeLLM(ai)
    out, rep = clean_texts(texts, rule, second, max_chars=10, checkpoint_file=ckpt)
    assert len(second.calls) == 2 and rep.reused_batches == 1  # 已完成的批次不再呼叫 AI
    assert all(v.startswith("第") for v in out.values())


def test_clean_fillers_for_document(monkeypatch: pytest.MonkeyPatch) -> None:
    segs = [RawSegment(start=0, end=1, speaker="講者1", text="嗯。"),
            RawSegment(start=1, end=2, speaker="講者1", text="呃，那個，對對對，我們開始。"),
            RawSegment(start=2, end=3, speaker="講者2", text="Um, we, uh, agree.")]
    messages: list[str] = []
    out, rep = tasks.clean_fillers(segs, load_config(), LanguageRegistry.load(), lambda p, m: messages.append(m))
    assert [s.text for s in out] == ["對，我們開始。", "we, agree."]  # 「嗯。」整段移除
    assert segs[0].text == "嗯。"  # 原始片段不變
    assert any("只用規則" in m for m in messages) and not rep.ai_used
