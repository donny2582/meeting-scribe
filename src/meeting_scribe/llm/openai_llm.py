"""OpenAI 實作（Chat Completions，REST）。

- 各工作的模型與推理強度由設定檔 ``[llm.openai.models]``、``[llm.openai.reasoning_effort]`` 指定，不寫死在程式中。
- 以結構化輸出（response_format = json_schema、strict）取得符合 schema 的 JSON。
- 不送 temperature；輸出上限使用 max_completion_tokens（含推理 token）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from meeting_scribe.config import get_secret
from meeting_scribe.llm.base import LLMClient, LLMError

log = logging.getLogger(__name__)


class OpenAILLM(LLMClient):
    """呼叫 OpenAI Chat Completions API。"""

    name = "openai"

    def __init__(self, cfg: dict[str, Any], api_key: str | None = None) -> None:
        super().__init__()
        key = api_key or get_secret("openai_api_key")
        if not key:
            raise LLMError("尚未設定 OpenAI API 金鑰：請在「API 金鑰」分頁輸入", fatal=True)
        llm_cfg = cfg.get("llm", {})
        own = llm_cfg.get("openai", {})
        self.key = key
        self.models = {k: str(v) for k, v in (own.get("models") or {}).items()}
        self.efforts = {k: str(v) for k, v in (own.get("reasoning_effort") or {}).items()}
        self.base_url = str(own.get("base_url", "https://api.openai.com/v1")).rstrip("/")
        self.timeout = float(llm_cfg.get("timeout_sec", 600))
        self.retries = int(llm_cfg.get("max_retries", 3))

    def complete_json(self, system: str, user: str, schema: dict[str, Any], task: str,
                      max_tokens: int = 16000) -> dict[str, Any]:
        model = self.model_for(task)
        if not model:
            raise LLMError(f"尚未設定「{task}」使用的 OpenAI 模型", fatal=True)
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": schema, "strict": True},
            },
            "max_completion_tokens": max_tokens,
        }
        if self.efforts.get(task):
            body["reasoning_effort"] = self.efforts[task]

        from meeting_scribe.stt.base import SttError, request_with_retry

        def send() -> httpx.Response:
            return httpx.post(f"{self.base_url}/chat/completions", json=body,
                              headers={"Authorization": f"Bearer {self.key}"},
                              timeout=httpx.Timeout(self.timeout, connect=30.0))

        try:
            resp = request_with_retry(send, self.retries, what="OpenAI")
        except SttError as exc:
            raise LLMError("無法連線到 OpenAI，請檢查網路", fatal=True) from exc
        if resp.status_code in (401, 403):
            raise LLMError("OpenAI API 金鑰無效或沒有權限", fatal=True)
        if resp.status_code == 404:
            raise LLMError(f"找不到模型「{model}」，請確認「API 金鑰」分頁的模型選擇", fatal=True)
        if resp.status_code == 429:
            raise LLMError("OpenAI 請求過多或額度不足，請稍後再試", fatal=True)
        if resp.status_code >= 400:
            raise LLMError(f"OpenAI 回應錯誤（HTTP {resp.status_code}）：{resp.text[:300]}", fatal=True)

        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        if message.get("refusal"):
            raise LLMError("模型拒絕處理這段內容")
        if choice.get("finish_reason") == "length":
            raise LLMError("模型輸出超過長度上限，請調小設定中的批次大小")
        if choice.get("finish_reason") == "content_filter":
            raise LLMError("內容被 OpenAI 的內容過濾擋下")
        try:
            result = json.loads(message.get("content") or "")
        except json.JSONDecodeError as exc:
            raise LLMError("模型回傳的內容不是有效的 JSON") from exc
        usage = data.get("usage") or {}
        log.info("OpenAI %s：輸入 %s、輸出 %s tokens", data.get("model", model),
                 usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"))
        return result
