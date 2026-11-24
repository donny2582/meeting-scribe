"""pydantic 資料結構（SPEC 5.1、6.1）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RawSegment(BaseModel):
    """STT 引擎輸出的原始片段（SPEC 5.1）。"""

    start: float = Field(description="開始時間（秒）")
    end: float = Field(description="結束時間（秒）")
    speaker: str = Field(description="講者代號；重新編號後為「講者N」")
    text: str
    confidence: float | None = None
    lang: str | None = Field(default=None, description="引擎回傳的語言（地區設定或代碼）")
    speaker_uncertain: bool = Field(default=False, description="跨段講者對應失敗等原因造成的不確定")


class SttResult(BaseModel):
    """一次語音轉文字的完整結果。"""

    engine: str
    engine_version: str
    audio_duration: float
    elapsed_sec: float
    locales: list[str]
    segments: list[RawSegment]
    raw_responses: list[dict] = Field(default_factory=list, description="引擎原始回應（完整保留）")
    script_conversion: str | None = Field(default=None, description="已套用的中文字形轉換，例：s2twp")
    needs_zh_tw_localization: bool = Field(
        default=False, description="尚待 LLM 做臺灣繁體在地化（見 postprocess/localize.py）"
    )
    localized_by: str | None = Field(default=None, description="已完成臺灣在地化的 LLM，例：anthropic claude-opus-5-5")
    clip_start: float = Field(default=0.0, description="只轉錄部分範圍時的起點（秒）；片段時間已加上此值，對應原始錄音")
    clip_end: float | None = Field(default=None, description="只轉錄部分範圍時的終點（秒）")


class Segment(BaseModel):
    """後處理後的片段（SPEC 6.1，第 1 階段使用）。"""

    id: str
    start: float
    end: float
    speaker: str
    speaker_uncertain: bool = False
    text_raw: str
    text_corrected: str = ""
    text_clean: str = ""
    lang: str = "zh"
    text_zh: str | None = None
    flags: list[str] = Field(default_factory=list)
    edited_by_user: bool = False
