"""OpenAI 文字整理處理（假的 HTTP 回應，不呼叫真實 API）與服務切換。"""

import json
from typing import Any

import httpx
import pytest

from meeting_scribe import tasks
from meeting_scribe.config import load_config
from meeting_scribe.llm import create_llm, provider_of
from meeting_scribe.llm.anthropic_llm import AnthropicLLM
from meeting_scribe.llm.base import LLMError
from meeting_scribe.llm.openai_llm import OpenAILLM
from meeting_scribe.models import RawSegment, SttResult


def _reply(content: Any = None, finish: str = "stop", refusal: str | None = None, status: int = 200):  # type: ignore[no-untyped-def]
    calls: list[dict[str, Any]] = []

    def post(url: str, json: dict, headers: dict, timeout: Any) -> httpx.Response:
        calls.append({"url": url, "json": json, "headers": headers})
        body = {"model": "gpt-6.2-sol", "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "choices": [{"finish_reason": finish, "message": {
                    "content": content if isinstance(content, str) else _dumps(content), "refusal": refusal}}]}
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    return post, calls


def _dumps(obj: Any) -> str:
    return json.dumps(obj if obj is not None else {"items": []}, ensure_ascii=False)


@pytest.fixture
def cfg() -> dict[str, Any]:
    c = load_config()
    c["llm"]["max_retries"] = 0
    return c


def test_openai_request_shape(monkeypatch: pytest.MonkeyPatch, cfg: dict) -> None:
    post, calls = _reply({"items": [{"id": "S0000", "text": "好"}]})
    monkeypatch.setattr(httpx, "post", post)
    llm = OpenAILLM(cfg, api_key="sk-test")
    assert llm.complete_json("sys", "user", {"type": "object"}) == {"items": [{"id": "S0000", "text": "好"}]}
    body = calls[0]["json"]
    assert calls[0]["url"].endswith("/chat/completions")
    assert body["model"] == cfg["llm"]["openai"]["model"]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["reasoning_effort"] == "low"
    assert "max_completion_tokens" in body and "temperature" not in body and "max_tokens" not in body
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-test"


@pytest.mark.parametrize("kwargs,match,fatal", [
    ({"refusal": "抱歉"}, "拒絕", False),
    ({"finish": "length"}, "長度上限", False),
    ({"finish": "content_filter"}, "過濾", False),
    ({"content": "not json"}, "JSON", False),
    ({"status": 401}, "金鑰", True),
    ({"status": 404}, "找不到模型", True),
    ({"status": 429}, "額度", True),
])
def test_openai_errors(monkeypatch: pytest.MonkeyPatch, cfg: dict, kwargs: dict, match: str, fatal: bool) -> None:
    post, _ = _reply(**kwargs)
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(LLMError, match=match) as err:
        OpenAILLM(cfg, api_key="sk-test").complete_json("s", "u", {})
    assert err.value.fatal is fatal


def test_provider_switch(monkeypatch: pytest.MonkeyPatch, cfg: dict) -> None:
    monkeypatch.setattr("meeting_scribe.llm.openai_llm.get_secret", lambda n: "sk-test")
    monkeypatch.setattr("meeting_scribe.llm.anthropic_llm.get_secret", lambda n: "sk-ant-test")
    assert provider_of(cfg) == "anthropic"
    assert isinstance(create_llm(cfg), AnthropicLLM)
    cfg["llm"]["provider"] = "openai"
    assert isinstance(create_llm(cfg), OpenAILLM)
    cfg["llm"]["provider"] = "unknown"
    assert provider_of(cfg) == "anthropic"


def test_localize_uses_selected_provider(monkeypatch: pytest.MonkeyPatch, cfg: dict) -> None:
    cfg["llm"]["provider"] = "openai"
    asked: list[str] = []
    monkeypatch.setattr(tasks, "get_secret", lambda n: asked.append(n) or "sk-test")
    monkeypatch.setattr("meeting_scribe.llm.openai_llm.get_secret", lambda n: "sk-test")
    post, calls = _reply({"items": [{"id": "S0000", "text": "這個專案"}]})
    monkeypatch.setattr(httpx, "post", post)
    result = SttResult(engine="soniox", engine_version="v", audio_duration=1, elapsed_sec=1, locales=["zh-TW"],
                       segments=[RawSegment(start=0, end=1, speaker="講者1", text="這個項目", lang="zh")],
                       script_conversion="s2twp", needs_zh_tw_localization=True)
    out = tasks.localize_if_possible(result, cfg, [], lambda p, m: None)
    assert asked == ["openai_api_key"]
    assert out.segments[0].text == "這個專案"
    assert out.localized_by == f"openai {cfg['llm']['openai']['model']}"
    assert len(calls) == 1
