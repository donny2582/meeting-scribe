"""Anthropic Claude 實作（官方 SDK）。

- 模型名稱、effort 由設定檔 ``[llm.anthropic]`` 指定，不寫死在程式中。
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
        key = api_key or get_secret("anthropic_api_key")
        if not key:
            raise LLMError("尚未設定 Anthropic API 金鑰：請在「API 金鑰」分頁輸入", fatal=True)
        llm_cfg = cfg.get("llm", {})
        own = llm_cfg.get("anthropic", {})
        self.model = str(own.get("model", ""))
        self.effort = own.get("effort") or None
        self.use_fallbacks = bool(own.get("server_side_fallback", True))
        self.client = anthropic.Anthropic(
            api_key=key,
            max_retries=int(llm_cfg.get("max_retries", 3)),
            timeout=float(llm_cfg.get("timeout_sec", 600)),
        )

    def complete_json(self, system: str, user: str, schema: dict[str, Any], max_tokens: int = 16000) -> dict[str, Any]:
        if not self.model:
            raise LLMError("尚未設定 Anthropic 模型名稱", fatal=True)
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if self.effort:
            output_config["effort"] = self.effort
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": output_config,
        }
        try:
            if self.use_fallbacks:
                response = self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
            else:
                response = self.client.messages.create(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise LLMError("Anthropic API 金鑰無效", fatal=True) from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMError(f"Anthropic API 金鑰沒有權限：{exc.message}", fatal=True) from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(f"找不到模型「{self.model}」，請確認「API 金鑰」分頁的模型名稱", fatal=True) from exc
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
        usage = response.usage
        log.info("Anthropic %s：輸入 %s、輸出 %s tokens", response.model,
                 getattr(usage, "input_tokens", "?"), getattr(usage, "output_tokens", "?"))
        return data
