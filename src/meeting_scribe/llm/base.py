"""LLM 介面（SPEC 7.5）：雲端或本機模型都實作同一個介面，模型名稱由設定檔指定。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class LLMError(Exception):
    """LLM 呼叫失敗（金鑰無效、額度不足、回應格式錯誤、被拒絕等）。

    fatal=True 表示重試也沒用（金鑰無效、額度不足、模型不存在、網路中斷），應立即停止。
    """

    def __init__(self, message: str, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


class LLMClient(ABC):
    """LLM 用戶端介面。"""

    name: str = ""

    @abstractmethod
    def complete_json(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        model: str,
        effort: str | None = None,
        max_tokens: int = 16000,
    ) -> dict[str, Any]:
        """送出一次請求，回傳符合 schema 的 JSON 物件。"""
