"""LLM 介面（SPEC 7.5）：文字整理處理用的服務都實作同一個介面。

每項工作（臺灣在地化、刪除贅字、外語翻譯、整理討論事項／結論／待辦）可以用不同模型，
模型名稱與推理強度由設定檔 ``[llm.<服務>.models]``、``[llm.<服務>.effort]`` 指定，不寫死在程式中。
"""

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

    def __init__(self) -> None:
        self.models: dict[str, str] = {}
        self.efforts: dict[str, str] = {}

    def model_for(self, task: str) -> str:
        """某項工作使用的模型名稱。"""
        return self.models.get(task, "")

    @abstractmethod
    def complete_json(self, system: str, user: str, schema: dict[str, Any], task: str,
                      max_tokens: int = 16000) -> dict[str, Any]:
        """以 task 指定的模型送出一次請求，回傳符合 schema 的 JSON 物件。"""
