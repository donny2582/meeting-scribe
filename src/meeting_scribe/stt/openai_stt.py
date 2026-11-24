"""OpenAI 具語者辨識的轉錄模型引擎。

API：POST {base_url}/audio/transcriptions（multipart），response_format=diarized_json。
單檔大小有上限，因此一律切段（預設每段 20 分鐘、重疊 30 秒）並轉成 mp3，
各段的講者代號再以重疊區域串接（SPEC 5.2）。模型名稱由設定檔指定。
"""

from __future__ import annotations

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


def parse_openai_response(data: dict[str, Any]) -> list[RawSegment]:
    """把 diarized_json 回應轉成片段清單（時間為該段的相對時間）。"""
    segments: list[RawSegment] = []
    for seg in data.get("segments", []):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        segments.append(
            RawSegment(
                start=round(float(seg.get("start", 0.0)), 2),
                end=round(float(seg.get("end", 0.0)), 2),
                speaker=str(seg.get("speaker", "?")),
                text=text,
                confidence=seg.get("confidence"),
                lang=seg.get("language") or data.get("language"),
            )
        )
    return segments


class OpenAISttEngine(SttEngine):
    """OpenAI 轉錄（具語者辨識）。"""

    name = "openai"
    display_name = "OpenAI 轉錄（語者辨識）"

    def version(self) -> str:
        return f"OpenAI {self.engine_cfg.get('model', '')}"

    def _transcribe_file(
        self, path: Path, key: str, locales: list[str], phrases: list[str]
    ) -> tuple[list[RawSegment], dict]:
        model = self.engine_cfg.get("model")
        if not model:
            raise SttError("尚未設定 OpenAI 模型名稱：請在設定檔 [stt.openai] 填寫 model")
        url = f"{self.engine_cfg.get('base_url', 'https://api.openai.com/v1').rstrip('/')}/audio/transcriptions"
        form: dict[str, str] = {
            "model": model,
            "response_format": self.engine_cfg.get("response_format", "diarized_json"),
            "chunking_strategy": "auto",
        }
        if len(locales) == 1:
            form["language"] = locales[0].split("-")[0]
        if phrases and self.engine_cfg.get("send_prompt", False):
            form["prompt"] = "、".join(phrases[:200])
        timeout = float(self.cfg.get("stt", {}).get("http_timeout_sec", 3600))

        def send() -> httpx.Response:
            with open(path, "rb") as fh:
                return httpx.post(
                    url,
                    headers={"Authorization": f"Bearer {key}"},
                    data=form,
                    files={"file": (path.name, fh, "audio/mpeg")},
                    timeout=httpx.Timeout(timeout, connect=30.0),
                )

        resp = request_with_retry(send, int(self.cfg.get("stt", {}).get("retries", 3)), what="OpenAI")
        raise_for_status(resp, "OpenAI")
        data = resp.json()
        return parse_openai_response(data), data

    def transcribe(
        self,
        audio_path: Path,
        locales: list[str],
        phrases: list[str],
        num_speakers: int | None = None,
        progress: ProgressCallback | None = None,
        work_dir: Path | None = None,
    ) -> SttResult:
        key = get_secret("openai_api_key")
        if not key:
            raise SttError("尚未設定 OpenAI 金鑰：請執行 python -m meeting_scribe set-key openai_api_key")
        report = self._progress(progress)
        t0 = time.perf_counter()
        duration = probe_duration(audio_path, self.ffmpeg)
        log.info("OpenAI 開始：長度 %.0f 秒、語言 %s", duration, ",".join(locales))
        with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
            segs, raws = self._run_chunked(
                audio_path, duration,
                float(self.engine_cfg.get("chunk_sec", 1200)),
                float(self.engine_cfg.get("overlap_sec", 30)),
                Path(tmp), ".mp3",
                lambda p: self._transcribe_file(p, key, locales, phrases),
                report,
                bitrate=self.engine_cfg.get("chunk_bitrate", "48k"),
                force_export=True,
                checkpoint_dir=work_dir,
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
