"""STT 引擎：以名稱建立引擎實例。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from meeting_scribe.stt.base import SttEngine, SttError

ENGINE_NAMES = ("azure", "openai", "soniox", "local")

#: 可以指定「預估講者人數」的引擎（Azure 快速轉錄的 maxSpeakers）。
#: Soniox 檔案轉錄沒有這個參數、OpenAI 轉錄也不接受，一律自動判斷；介面上會鎖定為「自動」避免誤會。
SPEAKER_COUNT_ENGINES = frozenset({"azure"})


def create_engine(name: str, cfg: dict[str, Any], ffmpeg: Path) -> SttEngine:
    """依名稱建立 STT 引擎。"""
    if name == "azure":
        from meeting_scribe.stt.azure_speech import AzureSpeechEngine

        return AzureSpeechEngine(cfg, ffmpeg)
    if name == "openai":
        from meeting_scribe.stt.openai_stt import OpenAISttEngine

        return OpenAISttEngine(cfg, ffmpeg)
    if name == "soniox":
        from meeting_scribe.stt.soniox_stt import SonioxSttEngine

        return SonioxSttEngine(cfg, ffmpeg)
    if name == "local":
        from meeting_scribe.stt.local_whisper import LocalWhisperEngine

        return LocalWhisperEngine(cfg, ffmpeg)
    raise SttError(f"未知的引擎：{name}（可用：{', '.join(ENGINE_NAMES)}）")
