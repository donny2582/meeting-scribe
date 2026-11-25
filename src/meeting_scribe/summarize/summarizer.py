"""依會議類型產生摘要（SPEC 第 7 章）。

流程：
1. 逐字稿每段加上編號：「S0012 [00:04:07] 講者1：內容」，讓每條結論可以回溯到片段。
2. 系統提示詞＝共通規則（prompts/common.txt）＋會議類型規則（topic／qa／agenda.txt）＋用語對照表＋詞庫。
3. 呼叫「整理討論事項、結論、待辦」模型（task="summary"），以結構化輸出取得 JSON，交給 pydantic 驗證；
   格式驗證失敗時把錯誤回饋給模型重試，最多 2 次，仍失敗就停止（不輸出半成品）。
4. 逐字稿超過設定門檻（預設 6 萬字）時改用「每 20 分鐘一段先整理 → 合併」。
5. 每一次 AI 結果都存檔（工作資料夾 _work\\summary_*.json）；內容、會議類型、模型都沒變時直接沿用，不重複計費。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from meeting_scribe.llm.base import LLMClient, LLMError
from meeting_scribe.models import Summary
from meeting_scribe.summarize.schema import summary_schema

log = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).parent / "prompts"
PROMPT_VERSION = "2026-11-24"  # 提示詞改版時更新，讓舊的暫存結果失效
CHUNK_SEC = 20 * 60
PROMPT_FIELDS = ("title", "chair", "agenda")  # user_prompt 會送給 AI 的會議資訊欄位
ProgressFn = Callable[[float, str], None]


class SummaryError(Exception):
    """摘要失敗（AI 回傳的格式一直不正確等）。"""


@dataclass
class Line:
    """一段逐字稿（送給 AI 的格式）。"""

    sid: str
    start: float
    text: str


def _hms(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def make_line(sid: str, start: float, speaker: str, text: str, uncertain: bool = False) -> Line:
    mark = "（疑似）" if uncertain else ""
    return Line(sid, start, f"{sid} [{_hms(start)}] {speaker}{mark}：{text}")


def _prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.txt").read_text(encoding="utf-8")


def system_prompt(meeting_type: str, reference: str) -> str:
    """共通規則＋會議類型規則＋用語對照表與詞庫。"""
    return "\n\n".join([_prompt("common"), _prompt(meeting_type), reference])


def user_prompt(info: dict[str, Any], speakers: list[str], lines: list[Line], note: str = "") -> str:
    """會議資訊＋講者清單＋（議程）＋逐字稿。"""
    head = [f"會議名稱：{info.get('title', '').strip() or '（未填）'}"]
    if info.get("chair"):
        head.append(f"主席：{info['chair']}（逐字稿中主席可能以講者編號出現，請依發言內容判斷；不確定時寫「主席」）")
    if info.get("agenda"):
        head.append("議程（請依此順序與文字整理）：\n" + "\n".join(f"- {a}" for a in info["agenda"]))
    head.append("逐字稿中的講者：" + "、".join(speakers))
    if note:
        head.append(note)
    head.append("逐字稿（每行開頭為片段編號，evidence 請填這些編號）：")
    return "\n".join(head) + "\n" + "\n".join(line.text for line in lines)


def _key(*parts: Any) -> str:
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _load(path: Path, key: str) -> Summary | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("key") != key:
        return None
    try:
        return Summary.model_validate(data["summary"])
    except (KeyError, ValidationError):
        return None


def _save(path: Path, key: str, summary: Summary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"key": key, "summary": summary.model_dump()}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(path)


def call_summary(llm: LLMClient, system: str, user: str, max_tokens: int, retries: int = 2) -> Summary:
    """呼叫 AI 並以 pydantic 驗證；格式錯誤時把錯誤回饋給 AI 重試（最多 retries 次）。"""
    schema = summary_schema()
    message = user
    last = ""
    for attempt in range(retries + 1):
        try:
            data = llm.complete_json(system, message, schema, task="summary", max_tokens=max_tokens)
            summary = Summary.model_validate(data)
            if not summary.conclusions and not summary.discussion_items:
                raise LLMError("沒有整理出任何討論事項或結論")
            return summary
        except LLMError as exc:
            if exc.fatal:
                raise
            last = str(exc)
        except ValidationError as exc:
            last = str(exc)[:800]
        log.warning("摘要格式不正確（第 %d 次）：%s", attempt + 1, last[:200])
        message = user + f"\n\n（上一次的輸出有錯誤，請修正後重新輸出完整 JSON：{last}）"
    raise SummaryError(f"AI 回傳的摘要格式一直不正確，已重試 {retries} 次：{last[:200]}")


def summarize(
    lines: list[Line], info: dict[str, Any], speakers: list[str], llm: LLMClient, cfg: dict[str, Any],
    reference: str = "", progress: ProgressFn | None = None, checkpoint_dir: Path | None = None,
) -> Summary:
    """產生摘要（必要時分段整理再合併）。"""
    meeting_type = info.get("meeting_type", "topic")
    llm_cfg = cfg.get("llm", {})
    max_chars = int(llm_cfg.get("max_chars_single_pass", 60000))
    max_tokens = int(llm_cfg.get("summary_max_tokens", 32000))
    model = llm.model_for("summary")
    system = system_prompt(meeting_type, reference)
    # 快取條件只包含送給 AI 的內容（user_prompt 用到的會議名稱、主席、議程）：改地點、散會時間、
    # 勾選產出項目等不影響 AI 結果的欄位時，沿用先前的結果，不重複計費。
    prompt_info = {k: info.get(k) for k in PROMPT_FIELDS}
    base_key = [PROMPT_VERSION, model, meeting_type, prompt_info, speakers, reference]
    legacy_key = [PROMPT_VERSION, model, meeting_type, info, speakers, reference]  # 舊版存的結果也能沿用

    def report(pct: float, msg: str) -> None:
        if progress:
            progress(pct, msg)

    final_path = checkpoint_dir / f"summary_{meeting_type}.json" if checkpoint_dir else None
    texts = [line.text for line in lines]
    final_key = _key(base_key, texts)
    if final_path and (cached := _load(final_path, final_key) or _load(final_path, _key(legacy_key, texts))):
        report(100, "沿用先前已完成的 AI 整理結果（內容、會議類型、模型都沒變，不重複計費）")
        return cached

    total = sum(len(line.text) for line in lines)
    if total <= max_chars:
        report(0, f"AI 整理討論事項、結論、待辦中（{len(lines)} 段、約 {total:,} 字，一次處理）")
        summary = call_summary(llm, system, user_prompt(info, speakers, lines), max_tokens)
    else:
        chunks: list[list[Line]] = []
        for line in lines:
            idx = int(line.start // CHUNK_SEC)
            if not chunks or idx != int(chunks[-1][0].start // CHUNK_SEC):
                chunks.append([])
            chunks[-1].append(line)
        report(0, f"逐字稿約 {total:,} 字，超過一次處理的上限，改為每 20 分鐘一段（共 {len(chunks)} 段）先整理再合併")
        parts: list[Summary] = []
        for n, chunk in enumerate(chunks, start=1):
            path = checkpoint_dir / f"summary_{meeting_type}_part{n:02d}.json" if checkpoint_dir else None
            chunk_texts = [line.text for line in chunk]
            key = _key(base_key, n, chunk_texts)
            if path and (cached := _load(path, key) or _load(path, _key(legacy_key, n, chunk_texts))):
                report(100 * n / (len(chunks) + 1), f"沿用已完成的第 {n}/{len(chunks)} 段整理（不重複計費）")
                parts.append(cached)
                continue
            report(100 * (n - 1) / (len(chunks) + 1),
                   f"AI 整理第 {n}/{len(chunks)} 段（{_hms(chunk[0].start)} 起）")
            note = (f"（這是全場逐字稿的第 {n}/{len(chunks)} 段，只整理這一段的內容；"
                    "長官裁示與交辦事項一樣要寫出來。）")
            part = call_summary(llm, system, user_prompt(info, speakers, chunk, note), max_tokens)
            if path:
                _save(path, key, part)
            parts.append(part)
        report(100 * len(chunks) / (len(chunks) + 1), "AI 合併各段整理結果")
        merged_user = (user_prompt(info, speakers, [], _prompt("merge")) + "\n"
                       + "\n".join(json.dumps(p.model_dump(exclude={"model"}), ensure_ascii=False) for p in parts))
        summary = call_summary(llm, system, merged_user, max_tokens)
    summary.meeting_type = meeting_type
    summary.model = model
    if final_path:
        _save(final_path, final_key, summary)
    report(100, "AI 整理完成")
    return summary
