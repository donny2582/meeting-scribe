"""臺灣繁體中文在地化（LLM）。

設定了文字整理處理服務（Anthropic 或 OpenAI）的金鑰時，凡是 ``SttResult.needs_zh_tw_localization`` 為 True 的轉錄結果
（目前為 Soniox），轉錄完成後立即執行本步驟，之後的詞庫校正、翻譯、贅字刪除都以本步驟的結果為準：

1. 只處理中文片段（日文、韓文片段不送出，英文片段 OpenCC 不會改動，也不送出）。
2. 輸入已先經 OpenCC s2twp 轉成繁體；LLM 再校潤成臺灣繁體中文與臺灣在地用語：
   修正 OpenCC 一對多轉錯的字、套用用語對照表（assets/term_map_tw.csv）與詞庫正確詞。
3. 只改字形與用語，不得增刪內容、改寫語意或調換順序；中文夾雜的英文術語維持原樣。
4. 程式端驗證：每批的片段編號必須完全相同、順序不變，且每段長度變化在合理範圍內；
   不通過時重試一次，仍失敗就保留 OpenCC 結果並寫入日誌（不記錄內容）。
5. 原始引擎文字保存在 stt_raw.json，不受影響。

沒有金鑰時跳過本步驟，只保留 OpenCC 轉換結果。
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from meeting_scribe.config import assets_dir
from meeting_scribe.llm.base import LLMClient, LLMError
from meeting_scribe.models import RawSegment, SttResult
from meeting_scribe.postprocess.glossary import GlossaryEntry
from meeting_scribe.postprocess.lang_detect import detect_language

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """你是臺灣政府機關的會議逐字稿校對員，負責把語音辨識結果校潤成「臺灣繁體中文」並使用臺灣在地用語。

輸入是一批逐字稿片段（JSON，每段有 id 與 text），文字已由程式從簡體自動轉成繁體，但可能還有：
- 一對多的繁簡字轉錯（例：「干部」「后面」「发表」類型的錯字、「里／裡」「台／臺」用法）。
- 中國用語未改成臺灣用語（請依下方用語對照表）。
- 機關、人名、術語的寫法與詞庫不一致（請統一成詞庫中的正確詞）。

規則：
1. 只能修改字形與用語，不得增加、刪除或改寫語意，不得摘要、不得補字，也不要刪除口語贅字（之後會由別的步驟處理）。
2. 片段之間不可合併、拆分或調換順序；每個 id 都要原樣回傳一次。
3. 中文句子裡夾雜的英文術語、縮寫、數字維持原樣（例：LiDAR、ADAS、ETC、115年）。
4. 「台／臺」：機關與正式名稱用「臺」（例：臺灣、臺北市），一律以詞庫寫法為準。
5. 沒有需要修改的片段，原文照抄回傳。
6. 只輸出指定格式的 JSON。"""


def load_term_map(path: Path | None = None) -> list[tuple[str, str, str]]:
    """讀取用語對照表（避免使用, 改用, 備註）。"""
    rows: list[tuple[str, str, str]] = []
    with open(path or assets_dir() / "term_map_tw.csv", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("避免使用") and row.get("改用"):
                rows.append((row["避免使用"].strip(), row["改用"].strip(), (row.get("備註") or "").strip()))
    return rows


def build_reference(term_map: list[tuple[str, str, str]], glossary: list[GlossaryEntry]) -> str:
    """組成提示詞中的用語對照表與詞庫段落。"""
    lines = ["【用語對照表（避免使用 → 改用）】"]
    for bad, good, note in term_map:
        lines.append(f"- {bad} → {good}" + (f"（{note}）" if note else ""))
    if glossary:
        lines.append("\n【詞庫正確詞】")
        lines.append("、".join(e.term for e in glossary))
    return "\n".join(lines)


def _is_chinese(seg: RawSegment) -> bool:
    engine_lang = (seg.lang or "").split("-")[0].lower()
    if engine_lang in ("ja", "ko", "en"):
        return False
    return detect_language(seg.text, engine_locale=seg.lang) in ("zh", "mixed")


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


def validate_batch(sent: list[tuple[str, str]], data: dict[str, Any], max_change: float = 0.3) -> dict[str, str]:
    """檢查 LLM 回傳：編號相同、順序不變、長度變化不超過 max_change。通過時回傳 {id: 新文字}。"""
    items = data.get("items")
    if not isinstance(items, list) or [i.get("id") for i in items] != [sid for sid, _ in sent]:
        raise LLMError("回傳的片段編號或順序與輸入不一致")
    result: dict[str, str] = {}
    for (sid, original), item in zip(sent, items, strict=True):
        text = str(item.get("text", "")).strip()
        if not text:
            raise LLMError("回傳了空白片段")
        if abs(len(text) - len(original)) > max(3, len(original) * max_change):
            raise LLMError("片段長度變化過大，可能被改寫")
        result[sid] = text
    return result


def localize_result(
    result: SttResult,
    llm: LLMClient,
    cfg: dict[str, Any],
    glossary: list[GlossaryEntry] | None = None,
    progress: ProgressFn | None = None,
) -> SttResult:
    """對轉錄結果的中文片段做臺灣在地化，回傳新的 SttResult。"""
    max_chars = int(cfg.get("llm", {}).get("localize_batch_chars", 3000))
    system = SYSTEM_PROMPT + "\n\n" + build_reference(load_term_map(), glossary or [])

    items = [(f"S{i:04d}", s.text) for i, s in enumerate(result.segments) if _is_chinese(s)]
    batches = _batches(items, max_chars)
    updated: dict[str, str] = {}
    failed = 0
    for n, batch in enumerate(batches, start=1):
        if progress:
            progress(100.0 * (n - 1) / max(len(batches), 1), f"AI 臺灣在地化中（第 {n}/{len(batches)} 批）")
        user = "請校潤以下片段：\n" + _to_json(batch)
        for attempt in (1, 2):
            try:
                data = llm.complete_json(system, user, SCHEMA, task="localize")
                updated.update(validate_batch(batch, data))
                break
            except LLMError as exc:
                if exc.fatal:
                    raise
                if attempt == 2:
                    failed += 1
                    log.warning("在地化第 %d 批未通過驗證，保留 OpenCC 結果：%s", n, exc)
    if progress:
        progress(100.0, "AI 臺灣在地化完成")

    segments = [
        s.model_copy(update={"text": updated.get(f"S{i:04d}", s.text)}) for i, s in enumerate(result.segments)
    ]
    changed = sum(1 for i, s in enumerate(result.segments) if updated.get(f"S{i:04d}", s.text) != s.text)
    log.info("臺灣在地化：%d 批、%d 段修改、%d 批保留 OpenCC 結果", len(batches), changed, failed)
    return result.model_copy(update={
        "segments": segments,
        "needs_zh_tw_localization": failed > 0,
        "localized_by": f"{llm.name} {llm.model_for('localize')}",
    })


def _to_json(batch: list[tuple[str, str]]) -> str:
    import json

    return json.dumps({"items": [{"id": sid, "text": text} for sid, text in batch]}, ensure_ascii=False)
