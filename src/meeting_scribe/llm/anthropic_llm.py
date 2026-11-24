"""Anthropic Claude 實作（官方 SDK）。

- 各工作的模型與 effort 由設定檔 ``[llm.anthropic.models]``、``[llm.anthropic.effort]`` 指定，不寫死在程式中。
- 以結構化輸出（output_config.format = json_schema）取得保證可解析的 JSON。
- 啟用伺服器端備援（server-side fallback）：主要模型拒絕回答時，API 會自動改用備援模型完成同一個請求。
- 目前的模型不接受 temperature 等取樣參數，因此不送出。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from meeting_scribe.config import get_secret
from meeting_scribe.llm.base import LLMClient, LLMError

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicLLM(LLMClient):
    """呼叫 Anthropic Messages API。"""

    name = "anthropic"

    def __init__(self, cfg: dict[str, Any], api_key: str | None = None) -> None:
        super().__init__()
        key = api_key or get_secret("anthropic_api_key")
        if not key:
            raise LLMError("尚未設定 Anthropic API 金鑰：請在「API 金鑰」分頁輸入", fatal=True)
        llm_cfg = cfg.get("llm", {})
        own = llm_cfg.get("anthropic", {})
        self.models = {k: str(v) for k, v in (own.get("models") or {}).items()}
        self.efforts = {k: str(v) for k, v in (own.get("effort") or {}).items()}
        self.use_fallbacks = bool(own.get("server_side_fallback", True))
        self.client = anthropic.Anthropic(
            api_key=key,
            max_retries=int(llm_cfg.get("max_retries", 3)),
            timeout=float(llm_cfg.get("timeout_sec", 600)),
        )

    def complete_json(self, system: str, user: str, schema: dict[str, Any], task: str,
                      max_tokens: int = 16000) -> dict[str, Any]:
        model = self.model_for(task)
        if not model:
            raise LLMError(f"尚未設定「{task}」使用的 Anthropic 模型", fatal=True)
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if self.efforts.get(task):
            output_config["effort"] = self.efforts[task]
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": output_config,
        }
        try:
            stream = max_tokens > 16000  # 輸出很長時用串流，避免 HTTP 逾時
            if self.use_fallbacks:
                if stream:
                    with self.client.beta.messages.stream(betas=[FALLBACK_BETA], fallbacks="default", **kwargs) as s:
                        response = s.get_final_message()
                else:
                    response = self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
            elif stream:
                with self.client.messages.stream(**kwargs) as s:
                    response = s.get_final_message()
            else:
                response = self.client.messages.create(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise LLMError("Anthropic API 金鑰無效", fatal=True) from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMError(f"Anthropic API 金鑰沒有權限：{exc.message}", fatal=True) from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(f"找不到模型「{model}」，請確認「API 金鑰」分頁的模型選擇", fatal=True) from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("Anthropic 請求過多或額度不足，請稍後再試", fatal=True) from exc
        except anthropic.BadRequestError as exc:
            raise LLMError(f"Anthropic 請求格式錯誤：{exc.message}", fatal=True) from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic 服務錯誤（HTTP {exc.status_code}）", fatal=True) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("無法連線到 Anthropic，請檢查網路", fatal=True) from exc

        if response.stop_reason == "refusal":
            category = getattr(getattr(response, "stop_details", None), "category", None)
            raise LLMError(f"模型拒絕處理這段內容（{category or '未說明原因'}）")
        if response.stop_reason == "max_tokens":
            raise LLMError("模型輸出超過長度上限，請調小設定中的批次大小")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError("模型回傳的內容不是有效的 JSON") from exc
        self._record(response, task)
        return data

    @staticmethod
    def _record(response: Any, task: str) -> None:
        """記錄用量（快取讀寫的 token 也算入輸入，費用估算偏保守）。"""
        from meeting_scribe import usage as usage_log

        u = response.usage
        tokens_in = sum(int(getattr(u, k, 0) or 0) for k in
                        ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        tokens_out = int(getattr(u, "output_tokens", 0) or 0)
        log.info("Anthropic %s：輸入 %s、輸出 %s tokens", response.model, tokens_in, tokens_out)
        usage_log.record_llm("Anthropic", str(response.model), task, tokens_in, tokens_out)
