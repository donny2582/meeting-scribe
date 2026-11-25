"""贅字刪除（SPEC 6.4）——只刪不改。

1. 規則層：依各語言的贅字清單刪除，並把重複的「對對對」縮成「對」。
   「那個」「就是說」這類也是正常用詞的贅字，只在後面接標點或停頓時才刪（避免把「那個方案」刪成「方案」）。
2. AI 層（clean_segments）：請 LLM 刪除口語贅詞與不必要的重複，嚴禁改寫、增加或調換文字。
3. 驗證：AI 的結果（忽略標點）必須是原文的子序列，否則改用規則層結果並記錄。
   每一批完成就存檔，中斷後再執行只補做剩下的批次。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_PUNCT_RUN = re.compile(r"([，。、！？,.!?；;：:])(?:\s*[，。、,；;])+")
_LEADING_PUNCT = re.compile(r"^[\s，。、,.；;：:！？!?]+")
_SPACES = re.compile(r"[ \t　]{2,}")


def _is_latin(word: str) -> bool:
    return all(ch.isascii() for ch in word)


def strip_punct(text: str) -> str:
    """移除標點符號與空白（供比對使用）。"""
    return "".join(
        ch for ch in text if not ch.isspace() and not unicodedata.category(ch).startswith(("P", "Z"))
    )


def remove_fillers_rule(text: str, fillers: Iterable[str], repeat_collapse: Iterable[str] = ()) -> str:
    """規則層贅字刪除。

    Args:
        text: 校正後的文字。
        fillers: 贅字清單（最長的優先比對）。
        repeat_collapse: 連續重複時縮成一個的詞，例如「對」。
    """
    result = text
    for word in repeat_collapse:
        if not word:
            continue
        sep = r"[\s，,、]*"
        flags = re.IGNORECASE if _is_latin(word) else 0
        pattern = re.compile(rf"(?:{re.escape(word)}{sep}){{1,}}{re.escape(word)}", flags)
        result = pattern.sub(word, result)
    for word in sorted({w for w in fillers if w}, key=len, reverse=True):
        if _is_latin(word):
            result = re.sub(rf"\b{re.escape(word)}\b[,]?\s*", "", result, flags=re.IGNORECASE)
        elif len(word) >= 2:  # 也是正常用詞（那個、就是說）：只刪後面接停頓或句尾的
            result = re.sub(rf"{re.escape(word)}(?=[\s，,、。！？!?；;…]|$)", "", result)
        else:
            result = result.replace(word, "")
    result = _PUNCT_RUN.sub(r"\1", result)
    result = _LEADING_PUNCT.sub("", result)
    result = _SPACES.sub(" ", result)
    return result.strip()


def is_subsequence(clean: str, source: str) -> bool:
    """檢查 clean（忽略標點與空白）是否為 source 的子序列。"""
    it = iter(strip_punct(source).lower())
    return all(ch in it for ch in strip_punct(clean).lower())


# ---- AI 層 ----
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
        "required": ["id", "text"], "additionalProperties": False,
    }}},
    "required": ["items"], "additionalProperties": False,
}

SYSTEM_PROMPT = """你是會議逐字稿的校對員，工作是「刪除贅字」，讓逐字稿好讀，但不能改變任何內容。

可以刪除：
- 口語贅詞：呃、嗯、啊、欸、喔（當語助詞時）、那個（當停頓時）、就是說、然後然後、對對對（保留一個「對」）等。
- 不必要的重複：說話時的重複與結巴，例如「我我我們」→「我們」、「這個這個方案」→「這個方案」、「再再大家」→「再大家」。
- 外語片段的贅詞：英文 uh、um、you know；日文 えーと、あの；韓文 음、어 等。

嚴格禁止：
- 改寫、換字、補字、調換順序；也不要修正錯字或辨識錯誤（之後由人工校對）。
- 刪除有意義的內容，例如「那個方案」的「那個」、肯定回答的「對」、數字、人名、單位名稱。
- 摘要或合併片段。

可以調整標點符號（例如刪掉多餘的逗號），讓句子更容易閱讀。
整段都是贅詞（例如只有「嗯。」）時，回傳空字串。每個 id 都要原樣回傳一次，順序不變。只輸出指定格式的 JSON。"""

ProgressFn = Callable[[float, str], None]


@dataclass
class CleanReport:
    """刪除贅字的統計（顯示在執行紀錄）。"""

    segments: int = 0
    rule_changed: int = 0
    ai_batches: int = 0
    reused_batches: int = 0
    ai_rejected: int = 0
    dropped: int = 0
    ai_used: bool = False
    ai_error: str = ""
    kept_indices: list[int] = field(default_factory=list)


def _batches(items: list[tuple[str, str]], max_chars: int) -> list[list[tuple[str, str]]]:
    out: list[list[tuple[str, str]]] = []
    cur: list[tuple[str, str]] = []
    size = 0
    for item in items:
        if cur and size + len(item[1]) > max_chars:
            out.append(cur)
            cur, size = [], 0
        cur.append(item)
        size += len(item[1])
    if cur:
        out.append(cur)
    return out


def _accept(ai_text: str, source: str) -> bool:
    """AI 結果驗證：只能刪字（子序列），長段落不可一次刪掉大半（避免被當成贅字整段刪除）。"""
    if not is_subsequence(ai_text, source):
        return False
    src, out = strip_punct(source), strip_punct(ai_text)
    return not (len(src) >= 20 and len(out) < len(src) * 0.4)


def clean_texts(
    texts: list[tuple[str, str]],
    rule: Callable[[str, str], str],
    llm: Any | None = None,
    max_chars: int = 3000,
    progress: ProgressFn | None = None,
    checkpoint_file: Path | None = None,
) -> tuple[dict[str, str], CleanReport]:
    """刪除贅字。texts＝[(編號, 文字)]；rule(編號, 文字) 為規則層。回傳（編號 → 清理後文字, 統計）。

    有 llm 時：規則層結果作為備案，AI 層結果通過驗證才採用；每批存檔，可續跑。
    AI 發生無法重試的錯誤（金鑰、額度）時停止呼叫 AI，其餘段落使用規則層結果。
    """
    from meeting_scribe.llm.base import LLMError

    report = CleanReport(segments=len(texts))
    rule_out = {sid: rule(sid, text) for sid, text in texts}
    report.rule_changed = sum(1 for sid, text in texts if rule_out[sid] != text)
    result = dict(rule_out)
    if llm is None:
        return _finish(result, report)

    report.ai_used = True
    saved: dict[str, dict[str, str]] = {}
    if checkpoint_file and checkpoint_file.exists():
        try:
            saved = json.loads(checkpoint_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
    batches = _batches([(sid, text) for sid, text in texts if strip_punct(text)], max_chars)
    for n, batch in enumerate(batches, start=1):
        if all(saved.get(sid, {}).get("src") == text for sid, text in batch):
            for sid, text in batch:
                out = saved[sid].get("out")  # None＝AI 沒有回傳這段 → 用規則層
                result[sid] = out if out is not None and _accept(out, text) else rule_out[sid]
            report.reused_batches += 1
            continue
        if progress:
            progress(100.0 * (n - 1) / len(batches), f"AI 刪除贅字中（第 {n}/{len(batches)} 批）")
        user = "請刪除以下片段的贅字：\n" + json.dumps(
            {"items": [{"id": sid, "text": text} for sid, text in batch]}, ensure_ascii=False)
        try:
            data = llm.complete_json(SYSTEM_PROMPT, user, SCHEMA, task="filler")
        except LLMError as exc:
            if exc.fatal:
                report.ai_error = str(exc)
                log.warning("刪除贅字的 AI 呼叫停止，其餘段落使用規則層：%s", exc)
                break
            log.warning("刪除贅字第 %d 批失敗，使用規則層：%s", n, exc)
            continue
        got = {str(i.get("id")): str(i.get("text", "")).strip() for i in data.get("items", [])
               if isinstance(i, dict)}
        for sid, text in batch:
            out = got.get(sid)
            if out is not None and _accept(out, text):
                result[sid] = out
            else:
                report.ai_rejected += 1  # 沒回傳或改寫了內容 → 用規則層
            saved[sid] = {"src": text, "out": out}  # 沒回傳的也記下，這一批下次直接沿用，不重複計費
        report.ai_batches += 1
        if checkpoint_file:
            checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = checkpoint_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
            tmp.replace(checkpoint_file)
    return _finish(result, report)


def _finish(result: dict[str, str], report: CleanReport) -> tuple[dict[str, str], CleanReport]:
    report.dropped = sum(1 for v in result.values() if not strip_punct(v))
    log.info("刪除贅字：%d 段、規則層改動 %d 段、AI %d 批（沿用 %d 批）、AI 結果未通過驗證 %d 段、整段刪除 %d 段",
             report.segments, report.rule_changed, report.ai_batches, report.reused_batches,
             report.ai_rejected, report.dropped)
    return result, report
