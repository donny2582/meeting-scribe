"""臺灣繁體在地化（假的 LLM，不呼叫真實 API）。"""

from types import SimpleNamespace
from typing import Any

import pytest

from meeting_scribe import tasks
from meeting_scribe.config import load_config
from meeting_scribe.llm.anthropic_llm import FALLBACK_BETA, AnthropicLLM
from meeting_scribe.llm.base import LLMClient, LLMError
from meeting_scribe.models import RawSegment, SttResult
from meeting_scribe.postprocess.glossary import GlossaryEntry
from meeting_scribe.postprocess.localize import SYSTEM_PROMPT, localize_result, validate_batch


def _result() -> SttResult:
    segs = [
        RawSegment(start=0, end=1, speaker="講者1", text="請公路局在後面發表數據", lang="zh"),
        RawSegment(start=1, end=2, speaker="講者2", text="Our team holds a patent.", lang="en"),
        RawSegment(start=2, end=3, speaker="講者3", text="日本の気象庁です", lang="ja"),
        RawSegment(start=3, end=4, speaker="講者1", text="這個項目的視頻要優化", lang="zh"),
    ]
    return SttResult(engine="soniox", engine_version="v", audio_duration=4, elapsed_sec=1, locales=["zh-TW"],
                     segments=segs, script_conversion="s2twp", needs_zh_tw_localization=True)


class FakeLLM(LLMClient):
    def __init__(self, replies: list[Any]) -> None:
        self.replies = replies
        self.calls: list[dict[str, Any]] = []

    def complete_json(self, system: str, user: str, schema: dict, model: str, effort: str | None = None,
                      max_tokens: int = 16000) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "model": model, "effort": effort})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


GOOD = {"items": [{"id": "S0000", "text": "請公路局在後面發表資料"}, {"id": "S0003", "text": "這個專案的影片要改善"}]}


def test_validate_batch() -> None:
    sent = [("S0000", "請公路局在後面發表數據")]
    assert validate_batch(sent, {"items": [{"id": "S0000", "text": "請公路局在後面發表資料"}]}) == {
        "S0000": "請公路局在後面發表資料"}
    with pytest.raises(LLMError):
        validate_batch(sent, {"items": [{"id": "S9999", "text": "x"}]})  # 編號不符
    with pytest.raises(LLMError):
        validate_batch(sent, {"items": [{"id": "S0000", "text": "請公路局在後面發表數據，並且補充很多原本沒有說過的內容"}]})
    with pytest.raises(LLMError):
        validate_batch(sent, {"items": [{"id": "S0000", "text": " "}]})


def test_localize_only_chinese_segments() -> None:
    llm = FakeLLM([GOOD])
    cfg = load_config()
    out = localize_result(_result(), llm, cfg, [GlossaryEntry("公路局", [])])
    assert [s.text for s in out.segments] == [
        "請公路局在後面發表資料", "Our team holds a patent.", "日本の気象庁です", "這個專案的影片要改善"]
    assert not out.needs_zh_tw_localization
    assert out.localized_by == f"anthropic {cfg['llm']['localize_model']}"
    call = llm.calls[0]
    assert "気象庁" not in call["user"] and "patent" not in call["user"]  # 日文、英文不送出
    assert "視頻 → 影片" in call["system"] and "公路局" in call["system"]  # 用語對照表與詞庫
    assert call["system"].startswith(SYSTEM_PROMPT[:20])
    assert call["effort"] == cfg["llm"]["localize_effort"]


def test_invalid_reply_retries_then_keeps_opencc() -> None:
    bad = {"items": [{"id": "S0000", "text": "x"}]}
    llm = FakeLLM([bad, bad])
    out = localize_result(_result(), llm, load_config())
    assert len(llm.calls) == 2
    assert out.segments[0].text == "請公路局在後面發表數據"  # 保留 OpenCC 結果
    assert out.needs_zh_tw_localization  # 仍標記未完成


def test_invalid_then_valid() -> None:
    llm = FakeLLM([{"items": []}, GOOD])
    out = localize_result(_result(), llm, load_config())
    assert out.segments[3].text == "這個專案的影片要改善" and not out.needs_zh_tw_localization


def test_fatal_error_stops() -> None:
    llm = FakeLLM([LLMError("Anthropic API 金鑰無效", fatal=True)])
    with pytest.raises(LLMError):
        localize_result(_result(), llm, load_config())
    assert len(llm.calls) == 1


def test_batches_split_by_chars() -> None:
    segs = [RawSegment(start=i, end=i + 1, speaker="講者1", text="我們開始開會" * 10, lang="zh") for i in range(5)]
    result = _result().model_copy(update={"segments": segs})
    cfg = load_config()
    cfg["llm"]["localize_batch_chars"] = 130  # 每批 2 段
    replies = [{"items": [{"id": f"S{i:04d}", "text": "我們開始開會" * 10} for i in ids]}
               for ids in ([0, 1], [2, 3], [4])]
    llm = FakeLLM(replies)
    localize_result(result, llm, cfg)
    assert len(llm.calls) == 3


def test_localize_if_possible_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "get_secret", lambda n: None)
    messages: list[str] = []
    out = tasks.localize_if_possible(_result(), load_config(), [], lambda p, m: messages.append(m))
    assert out.needs_zh_tw_localization
    assert any("未設定 Anthropic 金鑰" in m for m in messages)


def test_localize_if_possible_keeps_result_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "get_secret", lambda n: "sk-ant-test")

    def boom(*a: Any, **k: Any) -> Any:
        raise LLMError("Anthropic 請求過多或額度不足，請稍後再試", fatal=True)

    monkeypatch.setattr("meeting_scribe.postprocess.localize.localize_result", boom)
    messages: list[str] = []
    out = tasks.localize_if_possible(_result(), load_config(), [], lambda p, m: messages.append(m))
    assert out.segments[0].text == "請公路局在後面發表數據"
    assert any("失敗" in m for m in messages)


class _FakeMessages:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.response


def _response(stop_reason: str = "end_turn", text: str = '{"items": []}') -> Any:
    return SimpleNamespace(stop_reason=stop_reason, model="claude-opus-5-5",
                           content=[SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                           stop_details=SimpleNamespace(category="cyber"))


def _llm(response: Any) -> tuple[AnthropicLLM, _FakeMessages]:
    llm = AnthropicLLM(load_config(), api_key="sk-ant-test")
    fake = _FakeMessages(response)
    llm.client = SimpleNamespace(beta=SimpleNamespace(messages=fake), messages=fake)  # type: ignore[assignment]
    return llm, fake


def test_anthropic_request_shape() -> None:
    llm, fake = _llm(_response())
    assert llm.complete_json("sys", "user", {"type": "object"}, "claude-opus-5-5", "low") == {"items": []}
    kw = fake.kwargs
    assert kw["model"] == "claude-opus-5-5"
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert kw["output_config"] == {"format": {"type": "json_schema", "schema": {"type": "object"}}, "effort": "low"}
    assert "temperature" not in kw and "thinking" not in kw


def test_anthropic_refusal_and_bad_json() -> None:
    with pytest.raises(LLMError, match="拒絕"):
        _llm(_response("refusal"))[0].complete_json("s", "u", {}, "m")
    with pytest.raises(LLMError, match="JSON"):
        _llm(_response(text="not json"))[0].complete_json("s", "u", {}, "m")
    with pytest.raises(LLMError, match="長度上限"):
        _llm(_response("max_tokens"))[0].complete_json("s", "u", {}, "m")


def test_anthropic_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("meeting_scribe.llm.anthropic_llm.get_secret", lambda n: None)
    with pytest.raises(LLMError) as err:
        AnthropicLLM(load_config())
    assert err.value.fatal
