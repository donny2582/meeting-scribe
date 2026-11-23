"""Azure AI Speech 快速轉錄（Fast Transcription）引擎。

API：POST {endpoint}/speechtotext/transcriptions:transcribe?api-version=...
以 multipart/form-data 上傳 ``audio`` 與 ``definition``（JSON）。
長度或大小超過設定上限時自動切段，並以重疊區域串接講者（SPEC 5.2）。
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from meeting_scribe.audio.preprocess import probe_duration
from meeting_scribe.config import get_secret
from meeting_scribe.models import RawSegment, SttResult
from meeting_scribe.stt.base import (
    ProgressCallback,
    SttEngine,
    SttError,
    raise_for_status,
    renumber_speakers,
    request_with_retry,
)

log = logging.getLogger(__name__)


def parse_azure_response(data: dict[str, Any]) -> list[RawSegment]:
    """把快速轉錄的回應轉成片段清單（時間為該次請求的相對時間）。"""
    segments: list[RawSegment] = []
    for phrase in data.get("phrases", []):
        text = (phrase.get("text") or "").strip()
        if not text:
            continue
        start = phrase.get("offsetMilliseconds", 0) / 1000.0
        dur = phrase.get("durationMilliseconds", 0) / 1000.0
        segments.append(
            RawSegment(
                start=round(start, 2),
                end=round(start + dur, 2),
                speaker=str(phrase.get("speaker", 0)),
                text=text,
                confidence=phrase.get("confidence"),
                lang=phrase.get("locale"),
            )
        )
    return segments


class AzureSpeechEngine(SttEngine):
    """Azure AI Speech（zh-TW、語者辨識、詞彙清單）。"""

    name = "azure"
    display_name = "Azure AI Speech 快速轉錄"

    def version(self) -> str:
        return f"Azure AI Speech Fast Transcription（api-version {self.engine_cfg.get('api_version', '')}）"

    def _url(self) -> str:
        endpoint = (self.engine_cfg.get("endpoint") or "").rstrip("/")
        region = self.engine_cfg.get("region") or ""
        if not endpoint:
            if not region:
                raise SttError("尚未設定 Azure 區域：請在設定檔 [stt.azure] 填寫 region 或 endpoint")
            endpoint = f"https://{region}.api.cognitive.microsoft.com"
        return f"{endpoint}/speechtotext/transcriptions:transcribe"

    def _definition(self, locales: list[str], phrases: list[str], num_speakers: int | None) -> dict[str, Any]:
        definition: dict[str, Any] = {
            "locales": locales,
            "profanityFilterMode": "None",
            "diarization": {
                "enabled": True,
                "maxSpeakers": int(num_speakers or self.engine_cfg.get("max_speakers", 15)),
            },
        }
        if phrases and self.engine_cfg.get("send_phrase_list", True):
            definition["phraseList"] = {"phrases": phrases[: int(self.engine_cfg.get("max_phrases", 500))]}
        return definition

    def _transcribe_file(self, path: Path, key: str, definition: dict[str, Any]) -> tuple[list[RawSegment], dict]:
        url = self._url()
        timeout = float(self.cfg.get("stt", {}).get("http_timeout_sec", 3600))

        def send() -> httpx.Response:
            with open(path, "rb") as fh:
                return httpx.post(
                    url,
                    params={"api-version": self.engine_cfg.get("api_version", "2024-11-15")},
                    headers={"Ocp-Apim-Subscription-Key": key},
                    files={
                        "audio": (path.name, fh, "application/octet-stream"),
                        "definition": (None, json.dumps(definition, ensure_ascii=False), "application/json"),
                    },
                    timeout=httpx.Timeout(timeout, connect=30.0),
                )

        resp = request_with_retry(send, int(self.cfg.get("stt", {}).get("retries", 3)), what="Azure Speech")
        raise_for_status(resp, "Azure Speech")
        data = resp.json()
        return parse_azure_response(data), data

    def transcribe(
        self,
        audio_path: Path,
        locales: list[str],
        phrases: list[str],
        num_speakers: int | None = None,
        progress: ProgressCallback | None = None,
        work_dir: Path | None = None,
    ) -> SttResult:
        key = get_secret("azure_speech_key")
        if not key:
            raise SttError("尚未設定 Azure Speech 金鑰：請執行 python -m meeting_scribe set-key azure_speech_key")
        report = self._progress(progress)
        t0 = time.perf_counter()
        duration = probe_duration(audio_path, self.ffmpeg)
        size_mb = audio_path.stat().st_size / 1024 / 1024
        too_long = duration > float(self.engine_cfg.get("max_duration_sec", 7200))
        too_big = size_mb > float(self.engine_cfg.get("max_file_mb", 300))
        chunk_sec = float(self.engine_cfg.get("chunk_sec", 3600)) if (too_long or too_big) else duration + 1
        definition = self._definition(locales, phrases, num_speakers)
        log.info("Azure 開始：長度 %.0f 秒、%.1f MB、語言 %s", duration, size_mb, ",".join(locales))

        with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
            segs, raws = self._run_chunked(
                audio_path, duration, chunk_sec, float(self.engine_cfg.get("overlap_sec", 30)),
                Path(tmp), ".flac", lambda p: self._transcribe_file(p, key, definition), report,
            )
        return SttResult(
            engine=self.name,
            engine_version=self.version(),
            audio_duration=duration,
            elapsed_sec=time.perf_counter() - t0,
            locales=locales,
            segments=renumber_speakers(segs),
            raw_responses=raws,
        )
