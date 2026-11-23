"""STT 引擎介面（SPEC 5.1）。"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from meeting_scribe.models import RawSegment, SttResult

log = logging.getLogger(__name__)

#: 進度回呼：(百分比 0–100, 狀態文字)
ProgressCallback = Callable[[float, str], None]

RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class SttError(Exception):
    """語音轉文字失敗（金鑰無效、額度不足、網路錯誤等）。"""


def _noop_progress(_pct: float, _msg: str) -> None:
    return None


class SttEngine(ABC):
    """所有 STT 引擎共同實作的介面。"""

    name: str = ""
    display_name: str = ""

    def __init__(self, cfg: dict[str, Any], ffmpeg: Path) -> None:
        self.cfg = cfg
        self.engine_cfg: dict[str, Any] = cfg.get("stt", {}).get(self.name, {})
        self.ffmpeg = ffmpeg

    @abstractmethod
    def version(self) -> str:
        """引擎名稱與版本（寫入逐字稿表頭與評估報告）。"""

    @abstractmethod
    def transcribe(
        self,
        audio_path: Path,
        locales: list[str],
        phrases: list[str],
        num_speakers: int | None = None,
        progress: ProgressCallback | None = None,
        work_dir: Path | None = None,
    ) -> SttResult:
        """把音訊轉成片段清單。

        Args:
            audio_path: 已前處理的音訊檔。
            locales: 候選語言地區設定，例：["zh-TW", "en-US"]。
            phrases: 提示詞彙清單（由詞庫產生）。
            num_speakers: 預估講者人數，None 表示自動。
            progress: 進度回呼函式。
            work_dir: 暫存切段檔案的資料夾。
        """

    def price_per_hour(self) -> float:
        """設定中的單價（美元／音訊小時）。"""
        return float(self.engine_cfg.get("price_per_hour", 0.0) or 0.0)

    @staticmethod
    def _progress(progress: ProgressCallback | None) -> ProgressCallback:
        return progress or _noop_progress

    def _run_chunked(
        self,
        audio_path: Path,
        duration: float,
        chunk_sec: float,
        overlap_sec: float,
        work_dir: Path,
        suffix: str,
        transcribe_file: Callable[[Path], tuple[list[RawSegment], dict]],
        progress: ProgressCallback,
        bitrate: str | None = None,
        force_export: bool = False,
    ) -> tuple[list[RawSegment], list[dict]]:
        """切段（必要時）→ 逐段辨識 → 跨段串接講者。

        Args:
            transcribe_file: 辨識單一檔案，回傳（段內相對時間的片段, 原始回應）。
            force_export: 只有一段時也重新匯出（例如轉成較小的 mp3）。
        """
        from meeting_scribe.audio.preprocess import export_chunk
        from meeting_scribe.stt.chunk_merge import merge_chunk_results, plan_chunks

        spans = plan_chunks(duration, chunk_sec, overlap_sec)
        results = []
        raws: list[dict] = []
        for span in spans:
            progress(100.0 * span.index / len(spans), f"語音轉文字中（第 {span.index + 1}/{len(spans)} 段）")
            if len(spans) == 1 and not force_export:
                path = audio_path
            else:
                path = export_chunk(
                    audio_path, work_dir / f"chunk_{span.index:03d}{suffix}", self.ffmpeg,
                    span.start, span.duration, bitrate,
                )
            segs, raw = transcribe_file(path)
            results.append((span, segs))
            raws.append({"chunk": span.index, "offset": span.start, "response": raw})
            log.info("%s 第 %d/%d 段完成，%d 個片段", self.name, span.index + 1, len(spans), len(segs))
        progress(100.0, "語音轉文字完成")
        if len(spans) == 1:
            return results[0][1], raws
        return merge_chunk_results(results), raws


def renumber_speakers(segments: list[RawSegment]) -> list[RawSegment]:
    """依第一次發言的順序把講者代號重新編號為「講者1、講者2…」，並依時間排序。"""
    ordered = sorted(segments, key=lambda s: (s.start, s.end))
    mapping: dict[str, str] = {}
    out: list[RawSegment] = []
    for seg in ordered:
        if seg.speaker not in mapping:
            mapping[seg.speaker] = f"講者{len(mapping) + 1}"
        out.append(seg.model_copy(update={"speaker": mapping[seg.speaker]}))
    return out


def request_with_retry(
    send: Callable[[], httpx.Response], retries: int = 3, base_delay: float = 2.0, what: str = "API"
) -> httpx.Response:
    """以指數退避重試（SPEC 10：網路錯誤重試 3 次）。send 每次都要重新開檔送出。"""
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = send()
        except httpx.TransportError as exc:
            last_error = exc
            log.warning("%s 網路錯誤（第 %d 次）：%s", what, attempt + 1, type(exc).__name__)
        else:
            if resp.status_code not in RETRY_STATUS:
                return resp
            last_error = SttError(f"HTTP {resp.status_code}")
            log.warning("%s 回應 HTTP %d（第 %d 次）", what, resp.status_code, attempt + 1)
            if attempt == retries:
                return resp
        if attempt < retries:
            time.sleep(base_delay * (2**attempt))
    raise SttError(f"{what} 連線失敗，已重試 {retries} 次：{last_error}")


def raise_for_status(resp: httpx.Response, what: str) -> None:
    """把常見的 HTTP 錯誤轉成明確的中文訊息。"""
    if resp.status_code < 400:
        return
    detail = resp.text[:500]
    if resp.status_code in (401, 403):
        raise SttError(f"{what} 金鑰無效或沒有權限（HTTP {resp.status_code}）：{detail}")
    if resp.status_code == 429:
        raise SttError(f"{what} 請求過多或額度不足（HTTP 429）：{detail}")
    if resp.status_code == 413:
        raise SttError(f"{what} 檔案太大（HTTP 413），請調低設定中的 max_file_mb 或 chunk_sec")
    raise SttError(f"{what} 回應錯誤（HTTP {resp.status_code}）：{detail}")
