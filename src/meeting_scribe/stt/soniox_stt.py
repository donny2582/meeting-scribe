"""Soniox 非同步（檔案）語音轉文字引擎。

流程（REST，``https://api.soniox.com/v1``，``Authorization: Bearer``）：
1. ``POST /files`` 上傳音訊（multipart 欄位 ``file``）→ file_id
2. ``POST /transcriptions`` 建立轉錄（model、file_id、language_hints、語者辨識、語言辨識、context）
3. ``GET /transcriptions/{id}`` 輪詢狀態：queued → processing → completed／error
4. ``GET /transcriptions/{id}/transcript`` 取得逐字（token）結果
5. 刪除轉錄與檔案（不在 Soniox 保留資料；未刪除者 30 天後自動刪除）

單檔上限 300 分鐘，一般會議不需切段，講者代號全場一致。
Soniox 的中文只有 ``zh``，輸出不保證是臺灣繁體，因此中文片段一律先以 OpenCC 轉成臺灣繁體，
並標記 ``needs_zh_tw_localization``，讓第 1 階段的 LLM 先做臺灣在地化校潤。
"""

from __future__ import annotations

import logging
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from meeting_scribe.audio.preprocess import probe_duration
from meeting_scribe.config import get_secret
from meeting_scribe.models import RawSegment, SttResult
from meeting_scribe.postprocess.zh_tw import CONVERSION, to_zh_tw
from meeting_scribe.stt.base import (
    ProgressCallback,
    SttEngine,
    SttError,
    raise_for_status,
    renumber_speakers,
    request_with_retry,
)

log = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"[。！？!?.]\s*$")


def tokens_to_segments(
    tokens: list[dict[str, Any]], max_gap_sec: float = 1.0, soft_max_sec: float = 15.0
) -> list[RawSegment]:
    """把 Soniox 的逐字 token 組成片段。

    換講者、換語言、停頓超過 max_gap_sec 時分段；片段超過 soft_max_sec 後遇到句尾標點也分段。
    token 文字直接串接（英文單字開頭的 token 本身帶空白）。
    """
    segments: list[RawSegment] = []
    cur: list[dict[str, Any]] = []

    def flush() -> None:
        if not cur:
            return
        text = "".join(t.get("text", "") for t in cur).strip()
        if text:
            langs = [t.get("language") for t in cur if t.get("language")]
            confs = [float(t["confidence"]) for t in cur if t.get("confidence") is not None]
            segments.append(RawSegment(
                start=round(cur[0]["start_ms"] / 1000, 2),
                end=round(cur[-1]["end_ms"] / 1000, 2),
                speaker=str(cur[0].get("speaker") or "0"),
                text=text,
                confidence=round(sum(confs) / len(confs), 3) if confs else None,
                lang=max(set(langs), key=langs.count) if langs else None,
            ))
        cur.clear()

    for tok in tokens:
        if tok.get("is_audio_event") or tok.get("translation_status") == "translation":
            continue
        if cur:
            prev = cur[-1]
            seg_start = cur[0]["start_ms"] / 1000
            new_speaker = (tok.get("speaker") or "0") != (prev.get("speaker") or "0")
            new_lang = bool(tok.get("language") and prev.get("language")
                            and tok["language"] != prev["language"])
            gap = (tok["start_ms"] - prev["end_ms"]) / 1000 > max_gap_sec
            long_sentence = (prev["end_ms"] / 1000 - seg_start) >= soft_max_sec and bool(
                _SENTENCE_END.search(prev.get("text", "")))
            if new_speaker or new_lang or gap or long_sentence:
                flush()
        cur.append(tok)
    flush()
    return segments


def convert_chinese_segments(segments: list[RawSegment]) -> list[RawSegment]:
    """中文片段轉成臺灣繁體；日文、韓文、英文片段不轉換（避免日文漢字被改錯）。"""
    from meeting_scribe.postprocess.lang_detect import detect_language

    out = []
    for s in segments:
        engine_lang = (s.lang or "").split("-")[0].lower()
        rule_lang = detect_language(s.text, engine_locale=s.lang)
        if engine_lang not in ("ja", "ko") and rule_lang not in ("ja", "ko"):
            s = s.model_copy(update={"text": to_zh_tw(s.text)})  # OpenCC 不會改動英文
        out.append(s)
    return out


class SonioxSttEngine(SttEngine):
    """Soniox 非同步轉錄（語者辨識、語言辨識、context 詞彙）。"""

    name = "soniox"
    display_name = "Soniox 語音轉文字"

    def version(self) -> str:
        return f"Soniox {self.engine_cfg.get('model', '')}"

    # ---- HTTP ----
    def _base(self) -> str:
        return str(self.engine_cfg.get("base_url", "https://api.soniox.com/v1")).rstrip("/")

    def _request(self, method: str, path: str, key: str, what: str, upload: Path | None = None,
                 **kwargs: Any) -> httpx.Response:
        timeout = float(self.cfg.get("stt", {}).get("http_timeout_sec", 3600))

        def send() -> httpx.Response:
            if upload is not None:  # 每次重試都重新開檔
                with open(upload, "rb") as fh:
                    return httpx.request(method, f"{self._base()}{path}",
                                         headers={"Authorization": f"Bearer {key}"},
                                         files={"file": (upload.name, fh, "application/octet-stream")},
                                         timeout=httpx.Timeout(timeout, connect=30.0))
            return httpx.request(method, f"{self._base()}{path}", headers={"Authorization": f"Bearer {key}"},
                                 timeout=httpx.Timeout(60.0, connect=30.0), **kwargs)

        resp = request_with_retry(send, int(self.cfg.get("stt", {}).get("retries", 3)), what=f"Soniox {what}")
        if resp.status_code == 402:
            raise SttError("Soniox 帳戶餘額不足（HTTP 402），請到 console.soniox.com 儲值")
        raise_for_status(resp, f"Soniox {what}")
        return resp

    def _upload(self, path: Path, key: str) -> str:
        resp = self._request("POST", "/files", key, "上傳", upload=path)
        return str(resp.json()["id"])

    def _payload(self, file_id: str, locales: list[str], phrases: list[str]) -> dict[str, Any]:
        hints = list(dict.fromkeys(loc.split("-")[0].lower() for loc in locales))
        payload: dict[str, Any] = {
            "model": self.engine_cfg.get("model"),
            "file_id": file_id,
            "language_hints": hints,
            "enable_speaker_diarization": True,
            "enable_language_identification": True,
        }
        if self.engine_cfg.get("language_hints_strict"):
            payload["language_hints_strict"] = True
        context: dict[str, Any] = {}
        general = self.engine_cfg.get("context_general") or []
        if general:
            context["general"] = [{"key": g.get("key", ""), "value": g.get("value", "")} for g in general]
        if phrases:
            max_chars = int(self.engine_cfg.get("context_max_chars", 8000))
            terms, used = [], 0
            for p in phrases:
                if used + len(p) + 1 > max_chars:
                    break
                terms.append(p)
                used += len(p) + 1
            context["terms"] = terms
        if context:
            payload["context"] = context
        return payload

    def _wait(self, tid: str, key: str, report: ProgressCallback) -> None:
        interval = float(self.engine_cfg.get("poll_interval_sec", 3))
        deadline = time.monotonic() + float(self.engine_cfg.get("max_wait_sec", 7200))
        labels = {"queued": "排隊中", "processing": "辨識中"}
        while True:
            data = self._request("GET", f"/transcriptions/{tid}", key, "查詢狀態").json()
            status = data.get("status")
            if status == "completed":
                return
            if status == "error":
                raise SttError(f"Soniox 轉錄失敗：{data.get('error_type')}：{data.get('error_message')}")
            if time.monotonic() > deadline:
                raise SttError("Soniox 等候逾時，請稍後再試或調高設定中的 max_wait_sec")
            report(50 if status == "processing" else 10, f"Soniox {labels.get(status, status)}…")
            time.sleep(interval)

    def _cleanup(self, key: str, tid: str | None, file_id: str | None) -> None:
        """刪除 Soniox 上的轉錄與檔案；失敗只記錄（30 天後會自動刪除）。"""
        for path in ([f"/transcriptions/{tid}"] if tid else []) + ([f"/files/{file_id}"] if file_id else []):
            try:
                httpx.delete(f"{self._base()}{path}", headers={"Authorization": f"Bearer {key}"}, timeout=30)
            except httpx.HTTPError as exc:
                log.warning("Soniox 清除失敗（%s）：%s", path.split("/")[1], type(exc).__name__)

    # ---- 介面 ----
    def _transcribe_file(self, path: Path, key: str, locales: list[str], phrases: list[str],
                         report: ProgressCallback) -> tuple[list[RawSegment], dict]:
        if not self.engine_cfg.get("model"):
            raise SttError("尚未設定 Soniox 模型名稱：請在「服務與費用」分頁填寫")
        file_id: str | None = None
        tid: str | None = None
        try:
            report(5, "上傳音訊到 Soniox…")
            file_id = self._upload(path, key)
            resp = self._request("POST", "/transcriptions", key, "建立轉錄",
                                 json=self._payload(file_id, locales, phrases))
            tid = str(resp.json()["id"])
            self._wait(tid, key, report)
            data = self._request("GET", f"/transcriptions/{tid}/transcript", key, "取得結果").json()
        finally:
            if self.engine_cfg.get("delete_after", True):
                self._cleanup(key, tid, file_id)
        segs = tokens_to_segments(data.get("tokens", []), float(self.engine_cfg.get("segment_gap_sec", 1.0)))
        if self.engine_cfg.get("convert_zh_tw", True):
            segs = convert_chinese_segments(segs)
        return segs, {"id": data.get("id"), "tokens": data.get("tokens", [])}

    def transcribe(
        self,
        audio_path: Path,
        locales: list[str],
        phrases: list[str],
        num_speakers: int | None = None,
        progress: ProgressCallback | None = None,
        work_dir: Path | None = None,
    ) -> SttResult:
        key = get_secret("soniox_api_key")
        if not key:
            raise SttError("尚未設定 Soniox API 金鑰：請在「API 金鑰」分頁輸入")
        report = self._progress(progress)
        t0 = time.perf_counter()
        duration = probe_duration(audio_path, self.ffmpeg)
        max_dur = float(self.engine_cfg.get("max_duration_sec", 18000))
        chunk_sec = float(self.engine_cfg.get("chunk_sec", 14400)) if duration > max_dur else duration + 1
        log.info("Soniox 開始：長度 %.0f 秒、語言 %s", duration, ",".join(locales))
        with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
            segs, raws = self._run_chunked(
                audio_path, duration, chunk_sec, float(self.engine_cfg.get("overlap_sec", 30)), Path(tmp),
                ".flac", lambda p: self._transcribe_file(p, key, locales, phrases, report), report,
            )
        convert = bool(self.engine_cfg.get("convert_zh_tw", True))
        return SttResult(
            engine=self.name,
            engine_version=self.version(),
            audio_duration=duration,
            elapsed_sec=time.perf_counter() - t0,
            locales=locales,
            segments=renumber_speakers(segs),
            raw_responses=raws,
            script_conversion=CONVERSION if convert else None,
            needs_zh_tw_localization=bool(self.engine_cfg.get("llm_localize_zh_tw", True)),
        )
