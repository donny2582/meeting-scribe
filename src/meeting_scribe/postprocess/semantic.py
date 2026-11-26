"""產出文件前的「語意與角色上下文」校正建議（AI 建議 → 使用者確認後才套用）。

1. 文字校正：
   - 詞庫規則：「常見誤寫 → 正確詞」（不花費用）。
   - AI：依會議名稱、議程、前後文與詞庫，找出語音辨識造成的同音／近音錯字、專有名詞與機關名稱寫錯。
     規則嚴格：只修辨識錯誤，不改寫、不潤飾、不刪贅字；數字不可變動；修改幅度太大的建議會被擋下。
2. 講者角色：AI 依發言內容推測每位講者的身分（例：主席、公路局），附依據片段與把握度。

兩者都只產生「建議」，由使用者在確認視窗勾選後才套用（可以用「復原」取消）。
AI 結果每批存檔：中斷後再執行只補做剩下的批次；內容沒變時直接沿用，不重複計費。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from meeting_scribe.llm.base import LLMClient, LLMError
from meeting_scribe.postprocess.glossary import GlossaryEntry

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]
TASK = "correct"
CONFIDENCE = ("高", "中", "低")
PROMPT_VERSION = "2026-11-26"

FIX_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["id", "text", "reason"], "additionalProperties": False,
    }}},
    "required": ["items"], "additionalProperties": False,
}

ROLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"speakers": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "speaker": {"type": "string"},
            "name": {"type": "string"},
            "confidence": {"type": "string", "enum": list(CONFIDENCE)},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
        "required": ["speaker", "name", "confidence", "evidence", "reason"], "additionalProperties": False,
    }}},
    "required": ["speakers"], "additionalProperties": False,
}

FIX_PROMPT = """你是臺灣政府機關的會議逐字稿校對員。逐字稿由語音辨識產生，請依「會議資訊、前後文、詞庫」找出語音辨識造成的錯字並修正。

要修正的：
- 同音或近音的錯字（例：「獨架」→「毒駕」、「公路據」→「公路局」、「交通不」→「交通部」）。
- 機關、單位、人名、法規、計畫名稱與專業術語寫錯（以詞庫與會議資訊為準）。
- 依前後文明顯聽錯的詞。

嚴格禁止：
- 改寫、潤飾、調整語序、補字、摘要；不要修正口語文法；不要刪除贅字（另有步驟處理）。
- 改變數字、日期、金額（數字一律照抄）。
- 沒有把握的不要改。寧可少改，不要改錯。

只回傳需要修改的片段：id 照抄，text 是修改後的完整片段，reason 用一句話說明改了什麼（例：「獨架→毒駕：前後文在談毒品駕駛」）。
「前文」只供參考，不要回傳。沒有需要修改的片段時回傳空陣列。只輸出指定格式的 JSON。"""

ROLE_PROMPT = """你是臺灣政府機關的會議紀錄人員。逐字稿的講者只有代號（講者1、講者2…），請依發言內容推測每位講者的身分。

判斷依據（要有逐字稿中的具體發言）：
- 主持會議、宣布開始或散會、裁示、點名請單位說明的，通常是「主席」。
- 自稱單位（「公路局報告」「我們監理司這邊」）或被點名後回應的，用該單位名稱。
- 會議資訊中有主席、參加人員時，用那些名稱。

規則：
- name 寫身分或單位（例：主席、公路局、公共運輸及監理司），不要編造人名；沒有依據時 name 寫「無法判斷」。
- confidence：高＝有明確自稱或點名回應；中＝依發言內容推測；低＝只是猜測。
- evidence 填 1～3 個支持判斷的片段編號（例：S0012）。
- 每位講者都要回傳一次。只輸出指定格式的 JSON。"""


@dataclass
class TextFix:
    """一筆文字校正建議。"""

    index: int  # 片段位置（從 0 起算）
    original: str
    corrected: str
    reason: str
    source: str  # 「詞庫」或「AI」


@dataclass
class RoleSuggestion:
    """一筆講者角色建議。"""

    speaker: str  # 講者代號（例：講者1）
    name: str  # 建議名稱（例：主席）
    confidence: str  # 高／中／低
    evidence: list[int]  # 依據片段位置
    reason: str


@dataclass
class Suggestions:
    """AI 校正的結果（尚未套用）。"""

    fixes: list[TextFix] = field(default_factory=list)
    roles: list[RoleSuggestion] = field(default_factory=list)
    ai_used: bool = False
    batches: int = 0
    reused: int = 0
    rejected: int = 0
    notes: list[str] = field(default_factory=list)

    def empty(self) -> bool:
        return not self.fixes and not self.roles


# ---- 詞庫規則 ----
def glossary_fix(text: str, entries: list[GlossaryEntry]) -> tuple[str, list[str]]:
    """依詞庫「常見誤寫」取代，回傳（新文字, 取代說明）。最長的誤寫優先，不做模糊比對。"""
    pairs = sorted(((m, e.term) for e in entries for m in e.misspellings if m and m != e.term),
                   key=lambda p: len(p[0]), reverse=True)
    out: list[str] = []
    used: list[str] = []
    i = 0
    while i < len(text):
        for wrong, right in pairs:
            if text.startswith(wrong, i):
                out.append(right)
                if f"{wrong}→{right}" not in used:
                    used.append(f"{wrong}→{right}")
                i += len(wrong)
                break
        else:
            out.append(text[i])
            i += 1
    return "".join(out), used


# ---- AI 建議的驗證 ----
_DIGITS = re.compile(r"\d+")


def acceptable_fix(original: str, corrected: str) -> str | None:
    """檢查 AI 的修改是否只是小幅修正錯字；不通過時回傳原因。"""
    from rapidfuzz.distance import Levenshtein

    if not corrected.strip():
        return "修改後是空白"
    if corrected == original:
        return "沒有修改"
    if _DIGITS.findall(original) != _DIGITS.findall(corrected):
        return "數字被改動"
    distance = Levenshtein.distance(original, corrected)
    if distance > max(4, len(original) * 0.25):
        return "修改幅度太大（可能被改寫）"
    return None


def _key(*parts: Any) -> str:
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _load(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path | None, data: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _sid(index: int) -> str:
    return f"S{index + 1:04d}"


def _batches(items: list[tuple[int, str]], max_chars: int) -> list[list[tuple[int, str]]]:
    out: list[list[tuple[int, str]]] = []
    cur: list[tuple[int, str]] = []
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


def meeting_context(info: dict[str, Any], speakers: list[str]) -> str:
    """送給 AI 的會議資訊。"""
    lines = [f"會議名稱：{(info.get('title') or '').strip() or '（未填）'}"]
    for key, label in (("chair", "主席"), ("attendees", "參加人員"), ("location", "地點")):
        if (info.get(key) or "").strip():
            lines.append(f"{label}：{info[key].strip()}")
    if info.get("agenda"):
        lines.append("議程：" + "；".join(info["agenda"]))
    lines.append("講者：" + "、".join(speakers))
    return "\n".join(lines)


def suggest_fixes(
    lines: list[tuple[int, str, str, str]], info: dict[str, Any], speakers: list[str],
    entries: list[GlossaryEntry], reference: str, llm: LLMClient | None, max_chars: int = 2500,
    progress: ProgressFn | None = None, checkpoint_file: Path | None = None, result: Suggestions | None = None,
) -> Suggestions:
    """文字校正建議。lines＝[(片段位置, 時間碼, 講者名稱, 文字)]；llm 為 None 時只用詞庫規則。"""
    result = result or Suggestions()
    rule_text: dict[int, str] = {}
    rule_reason: dict[int, list[str]] = {}
    for idx, _stamp, _spk, text in lines:
        fixed, used = glossary_fix(text, entries)
        rule_text[idx] = fixed
        if used:
            rule_reason[idx] = used
    ai_text: dict[int, tuple[str, str]] = {}
    if llm is not None:
        result.ai_used = True
        system = FIX_PROMPT + "\n\n" + reference
        context = meeting_context(info, speakers)
        saved = _load(checkpoint_file)
        by_idx = {idx: (stamp, spk) for idx, stamp, spk, _ in lines}
        batches = _batches([(idx, rule_text[idx]) for idx, *_ in lines if rule_text[idx].strip()], max_chars)
        model = llm.model_for(TASK)
        prev: list[tuple[int, str]] = []
        for n, batch in enumerate(batches, start=1):
            def fmt(item: tuple[int, str]) -> str:
                stamp, spk = by_idx[item[0]]
                return f"{_sid(item[0])} [{stamp}] {spk}：{item[1]}"

            key = _key(PROMPT_VERSION, model, context, reference, [fmt(i) for i in prev[-3:]], [fmt(i) for i in batch])
            if key in saved:
                items = saved[key]
                result.reused += 1
            else:
                if progress:
                    progress(100.0 * (n - 1) / len(batches), f"AI 語意校正中（第 {n}/{len(batches)} 批）")
                user = (context + "\n\n"
                        + ("前文（只供參考，不要回傳）：\n" + "\n".join(fmt(i) for i in prev[-3:]) + "\n\n" if prev else "")
                        + "請校對以下片段：\n" + "\n".join(fmt(i) for i in batch))
                try:
                    data = llm.complete_json(system, user, FIX_SCHEMA, task=TASK)
                except LLMError as exc:
                    if exc.fatal:
                        raise
                    log.warning("語意校正第 %d 批失敗，略過：%s", n, exc)
                    result.notes.append(f"第 {n} 批 AI 回應無法使用，已略過")
                    prev = batch
                    continue
                items = [i for i in data.get("items", []) if isinstance(i, dict)]
                saved[key] = items
                _save(checkpoint_file, saved)
                result.batches += 1
            ids = {_sid(idx): idx for idx, _ in batch}
            source = dict(batch)
            for item in items:
                idx = ids.get(str(item.get("id", "")))
                if idx is None:
                    continue
                corrected = str(item.get("text", "")).strip()
                problem = acceptable_fix(source[idx], corrected)
                if problem:
                    if problem != "沒有修改":
                        result.rejected += 1
                        log.info("語意校正建議未通過驗證（%s）", problem)
                    continue
                ai_text[idx] = (corrected, str(item.get("reason", "")).strip())
            prev = batch
    for idx, _stamp, _spk, text in lines:
        final = ai_text[idx][0] if idx in ai_text else rule_text[idx]
        if final == text:
            continue
        reasons = []
        if idx in rule_reason:
            reasons.append("詞庫：" + "、".join(rule_reason[idx]))
        if idx in ai_text and ai_text[idx][1]:
            reasons.append(ai_text[idx][1])
        source = "AI" if idx in ai_text else "詞庫"
        if idx in ai_text and idx in rule_reason:
            source = "詞庫＋AI"
        result.fixes.append(TextFix(idx, text, final, "；".join(reasons), source))
    return result


def suggest_roles(
    lines: list[tuple[int, str, str, str]], info: dict[str, Any], speakers: list[str], named: set[str],
    llm: LLMClient, max_chars: int = 40000, checkpoint_file: Path | None = None, result: Suggestions | None = None,
) -> Suggestions:
    """講者角色建議。lines 的講者欄是講者代號；named＝使用者已命名的講者（不再建議）。"""
    result = result or Suggestions()
    targets = [s for s in speakers if s not in named]
    if not targets:
        return result
    per_speaker: dict[str, int] = {}
    chosen: list[tuple[int, str, str, str]] = []
    total = 0
    for line in lines:  # 依時間順序取樣；每位講者至少保留前 10 段
        spk = line[2]
        long = total + len(line[3]) > max_chars
        if long and per_speaker.get(spk, 0) >= 10:
            continue
        chosen.append(line)
        per_speaker[spk] = per_speaker.get(spk, 0) + 1
        total += len(line[3])
    text = "\n".join(f"{_sid(i)} [{stamp}] {spk}：{t}" for i, stamp, spk, t in chosen)
    user = (meeting_context(info, speakers) + "\n\n請推測這些講者的身分：" + "、".join(targets)
            + "\n\n逐字稿：\n" + text)
    model = llm.model_for(TASK)
    saved = _load(checkpoint_file)
    key = _key(PROMPT_VERSION, model, "roles", user)
    if key in saved:
        data = saved[key]
        result.reused += 1
    else:
        data = llm.complete_json(ROLE_PROMPT, user, ROLE_SCHEMA, task=TASK)
        saved[key] = data
        _save(checkpoint_file, saved)
        result.batches += 1
    result.ai_used = True
    valid_ids = {_sid(i): i for i, *_ in lines}
    for item in data.get("speakers", []):
        if not isinstance(item, dict):
            continue
        spk, name = str(item.get("speaker", "")).strip(), str(item.get("name", "")).strip()
        if spk not in targets or not name or name == spk or "無法判斷" in name:
            continue
        evidence = [valid_ids[e] for e in item.get("evidence", []) if e in valid_ids][:3]
        confidence = item.get("confidence") if item.get("confidence") in CONFIDENCE else "低"
        if not evidence:
            confidence = "低"
        result.roles.append(RoleSuggestion(spk, name, str(confidence), evidence, str(item.get("reason", "")).strip()))
    return result


def apply_suggestions(doc: Any, fixes: list[TextFix], roles: list[RoleSuggestion]) -> tuple[int, int]:
    """把使用者勾選的建議套用到文件，回傳（套用的文字校正數, 套用的講者命名數）。

    片段文字已經和建議時不同（例如之後又手動修改過）的建議會略過，避免蓋掉使用者的修改。
    """
    applied = 0
    for fix in fixes:
        if 0 <= fix.index < len(doc.segments) and doc.segments[fix.index].text == fix.original:
            doc.segments[fix.index] = doc.segments[fix.index].model_copy(update={"text": fix.corrected})
            applied += 1
    named = 0
    for role in roles:
        if role.speaker in doc.speakers() and role.name.strip():
            doc.rename_speaker(role.speaker, role.name)
            named += 1
    if applied or named:
        doc.edited = True
    return applied, named
