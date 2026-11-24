"""文字整理處理（LLM）服務：以設定 ``[llm] provider`` 選擇 Anthropic 或 OpenAI。"""

from __future__ import annotations

from typing import Any

from meeting_scribe.llm.base import LLMClient, LLMError

#: 服務代碼 → （顯示名稱, 金鑰名稱）
LLM_PROVIDERS: dict[str, tuple[str, str]] = {
    "anthropic": ("Anthropic Claude", "anthropic_api_key"),
    "openai": ("OpenAI", "openai_api_key"),
}


def provider_of(cfg: dict[str, Any]) -> str:
    """目前選用的文字整理服務代碼。"""
    provider = str(cfg.get("llm", {}).get("provider", "anthropic"))
    return provider if provider in LLM_PROVIDERS else "anthropic"


def create_llm(cfg: dict[str, Any]) -> LLMClient:
    """依設定建立文字整理服務的用戶端。"""
    if provider_of(cfg) == "openai":
        from meeting_scribe.llm.openai_llm import OpenAILLM

        return OpenAILLM(cfg)
    from meeting_scribe.llm.anthropic_llm import AnthropicLLM

    return AnthropicLLM(cfg)


__all__ = ["LLM_PROVIDERS", "LLMClient", "LLMError", "create_llm", "provider_of"]
