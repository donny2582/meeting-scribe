"""第 0 階段引擎評估（SPEC 5.3）。

標準答案（reference）為 CSV（UTF-8，可含 BOM），欄位：
``start,end,speaker,lang,text``；時間可寫秒數（12.5）或時碼（00:00:12.50）。
可先用 ``make-reference`` 由引擎結果產生草稿，再人工校正。

指標：
- 字元錯誤率（CER）：雙方都先以規則刪除贅字、移除標點與空白，再算編輯距離 ÷ 標準答案字數。
- 講者錯誤比例：以 10 毫秒為單位，在標準答案只有一人發言的時間內，
  把引擎講者對應到標準答案講者（依重疊時間貪婪配對）後，講者不符或漏掉的時間比例。
- 詞庫命中率：標準答案中出現的詞庫詞，引擎結果也正確出現的比例。
- 語言判定正確率：標準答案標為外語（en／ja／ko）的片段，引擎結果（重疊最多的片段，
  經字元規則判定）語言相同的比例。
- 中文誤判率：標準答案標為中文的片段，被判定為外語的比例。
- 處理時間、費用：換算為 2 小時錄音。

沒有標準答案時（快速比較）：只計算處理時間、費用、講者數、詞庫詞彙出現次數與引擎之間的差異率，
並產生逐段對照表（每 30 秒一列，各引擎的文字並排），供人工判斷哪個引擎比較準。
"""

from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from meeting_scribe.models import RawSegment, SttResult
from meeting_scribe.postprocess.filler import remove_fillers_rule, strip_punct
from meeting_scribe.postprocess.lang_detect import MIXED, LanguageRegistry, detect_language

TWO_HOURS = 7200.0
_TIME_RE = re.compile(r"^(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$")


@dataclass
class RefSegment:
    """標準答案片段。"""

    start: float
    end: float
    speaker: str
    lang: str
    text: str


def parse_time(value: str) -> float:
    """把「秒數」或「時:分:秒」轉成秒。"""
    value = value.strip()
    m = _TIME_RE.match(value)
    if m:
        h, mi, s = m.groups()
        return int(h or 0) * 3600 + int(mi) * 60 + float(s)
    return float(value)


def format_time(sec: float) -> str:
    """秒 → 時:分:秒.百分秒。"""
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:05.2f}"


def load_reference(path: Path) -> list[RefSegment]:
    """讀取標準答案 CSV（UTF-8；Excel 存成「CSV（逗號分隔）」時為 Big5，也可以讀）。"""
    import io

    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp950", errors="replace")
    out: list[RefSegment] = []
    with io.StringIO(text, newline="") as f:
        for row in csv.DictReader(f):
            if not (row.get("text") or "").strip():
                continue
            out.append(
                RefSegment(
                    start=parse_time(row["start"]),
                    end=parse_time(row["end"]),
                    speaker=(row.get("speaker") or "").strip(),
                    lang=(row.get("lang") or "zh").strip() or "zh",
                    text=row["text"].strip(),
                )
            )
    return sorted(out, key=lambda r: r.start)


def write_reference_draft(segments: list[RawSegment], path: Path, registry: LanguageRegistry) -> None:
    """把引擎結果寫成標準答案草稿 CSV（UTF-8 含 BOM，Excel 可直接開啟）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["start", "end", "speaker", "lang", "text"])
        for s in segments:
            lang = detect_language(s.text, registry, s.lang)
            w.writerow([format_time(s.start), format_time(s.end), s.speaker, lang, s.text])


def normalize_for_cer(text: str, registry: LanguageRegistry) -> str:
    """CER 比對前的正規化：刪除各語言贅字、全半形統一、移除標點與空白、小寫。"""
    t = unicodedata.normalize("NFKC", text)
    for rule in registry.languages.values():
        t = remove_fillers_rule(t, rule.fillers, rule.repeat_collapse)
    return strip_punct(t).lower()


def edit_distance(a: str, b: str) -> int:
    """編輯距離（有 rapidfuzz 時使用，否則以純 Python 計算）。"""
    try:
        from rapidfuzz.distance import Levenshtein

        return int(Levenshtein.distance(a, b))
    except ImportError:  # pragma: no cover - rapidfuzz 為必要套件，僅作備援
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            cur = [i]
            for j, cb in enumerate(b, 1):
                cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
            prev = cur
        return prev[-1]


def cer(ref_text: str, hyp_text: str, registry: LanguageRegistry) -> float:
    """字元錯誤率。"""
    r = normalize_for_cer(ref_text, registry)
    h = normalize_for_cer(hyp_text, registry)
    if not r:
        return 0.0 if not h else 1.0
    return edit_distance(r, h) / len(r)


def _frames(segs: list[tuple[float, float, str]], n: int, step: float) -> list[set[str]]:
    frames: list[set[str]] = [set() for _ in range(n)]
    for start, end, spk in segs:
        for i in range(max(0, int(start / step)), min(n, int(end / step))):
            frames[i].add(spk)
    return frames


def speaker_error_ratio(ref: list[RefSegment], hyp: list[RawSegment], step: float = 0.01) -> float:
    """講者錯誤比例（標準答案只有一人發言的時間為分母）。"""
    end = max([r.end for r in ref] + [h.end for h in hyp] + [0.0])
    n = int(end / step) + 1
    rf = _frames([(r.start, r.end, r.speaker) for r in ref], n, step)
    hf = _frames([(h.start, h.end, h.speaker) for h in hyp], n, step)

    overlap: dict[tuple[str, str], int] = {}
    for r_set, h_set in zip(rf, hf):
        for r in r_set:
            for h in h_set:
                overlap[(h, r)] = overlap.get((h, r), 0) + 1
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for (h, r), _ in sorted(overlap.items(), key=lambda kv: kv[1], reverse=True):
        if h not in mapping and r not in used:
            mapping[h] = r
            used.add(r)

    total = wrong = 0
    for r_set, h_set in zip(rf, hf):
        if len(r_set) != 1:
            continue
        total += 1
        (r,) = r_set
        if r not in {mapping.get(h) for h in h_set}:
            wrong += 1
    return wrong / total if total else 0.0


def glossary_hits(ref_text: str, hyp_text: str, terms: list[str]) -> tuple[int, int]:
    """回傳（命中次數, 標準答案中出現次數）。"""
    def norm(t: str) -> str:
        return unicodedata.normalize("NFKC", t).lower().replace(" ", "")

    r, h = norm(ref_text), norm(hyp_text)
    hits = total = 0
    for term in {norm(t) for t in terms if t}:
        n_ref = r.count(term)
        if n_ref:
            total += n_ref
            hits += min(n_ref, h.count(term))
    return hits, total


def language_metrics(
    ref: list[RefSegment], hyp: list[RawSegment], registry: LanguageRegistry
) -> tuple[tuple[int, int], tuple[int, int]]:
    """回傳（(外語判定正確數, 外語片段數), (中文誤判數, 中文片段數)）。"""
    base = registry.base_code
    foreign_ok = foreign_total = zh_wrong = zh_total = 0
    for r in ref:
        best: RawSegment | None = None
        best_ov = 0.0
        for h in hyp:
            ov = min(r.end, h.end) - max(r.start, h.start)
            if ov > best_ov:
                best, best_ov = h, ov
        hyp_lang = detect_language(best.text, registry, best.lang) if best else "unknown"
        if r.lang == base:
            zh_total += 1
            if hyp_lang not in (base, "unknown", "nan"):
                zh_wrong += 1
        elif r.lang in registry.languages and r.lang != MIXED:
            foreign_total += 1
            if hyp_lang == r.lang:
                foreign_ok += 1
    return (foreign_ok, foreign_total), (zh_wrong, zh_total)


@dataclass
class EngineReport:
    """單一引擎的評估結果。"""

    engine: str
    version: str
    audio_duration: float
    elapsed_sec: float
    price_per_hour: float
    cer: float | None = None
    speaker_error: float | None = None
    glossary: tuple[int, int] = (0, 0)
    lang_acc: tuple[int, int] = (0, 0)
    zh_misjudge: tuple[int, int] = (0, 0)
    n_speakers: int = 0
    has_reference: bool = True
    glossary_found: int = 0  # 詞庫詞彙在辨識結果中出現的次數（不需標準答案）
    norm_text: str = ""  # 正規化後的辨識文字，用來計算引擎之間的差異（不寫入報告）
    notes: list[str] = field(default_factory=list)

    @property
    def minutes_for_2h(self) -> float:
        return self.elapsed_sec / max(self.audio_duration, 1.0) * TWO_HOURS / 60

    @property
    def cost_for_2h(self) -> float:
        return self.price_per_hour * 2


def glossary_found(hyp_text: str, terms: list[str]) -> int:
    """詞庫詞彙在辨識結果中出現的次數（不需標準答案，次數越多通常代表專有名詞辨識越好）。"""
    h = unicodedata.normalize("NFKC", hyp_text).lower().replace(" ", "")
    return sum(h.count(t) for t in {unicodedata.normalize("NFKC", x).lower().replace(" ", "") for x in terms if x})


def evaluate(
    result: SttResult,
    ref: list[RefSegment] | None,
    registry: LanguageRegistry,
    terms: list[str],
    price_per_hour: float,
    lang_result: SttResult | None = None,
    lang_ref: list[RefSegment] | None = None,
) -> EngineReport:
    """計算一個引擎的所有指標；ref 為 None 時只算不需要標準答案的指標（快速比較）。"""
    hyp_text = "".join(s.text for s in result.segments)
    rep = EngineReport(
        engine=result.engine,
        version=result.engine_version,
        audio_duration=result.audio_duration,
        elapsed_sec=result.elapsed_sec,
        price_per_hour=price_per_hour,
        n_speakers=len({s.speaker for s in result.segments}),
        has_reference=ref is not None,
        glossary_found=glossary_found(hyp_text, terms),
        norm_text=normalize_for_cer(hyp_text, registry),
    )
    if any(s.speaker_uncertain for s in result.segments):
        n = sum(1 for s in result.segments if s.speaker_uncertain)
        rep.notes.append(f"切段串接時有 {n} 個片段的講者對應失敗（已標疑似）")
    if ref is None:
        return rep
    ref_text = "".join(r.text for r in ref)
    rep.cer = cer(ref_text, hyp_text, registry)
    rep.speaker_error = speaker_error_ratio(ref, result.segments)
    rep.glossary = glossary_hits(ref_text, hyp_text, terms)

    acc, zh = language_metrics(ref, result.segments, registry)
    if lang_result is not None and lang_ref is not None:
        acc2, zh2 = language_metrics(lang_ref, lang_result.segments, registry)
        acc = (acc[0] + acc2[0], acc[1] + acc2[1])
        zh = (zh[0] + zh2[0], zh[1] + zh2[1])
    rep.lang_acc, rep.zh_misjudge = acc, zh
    return rep


def difference_rate(a: str, b: str) -> float | None:
    """兩個引擎正規化文字的差異率（編輯距離 ÷ 較長者字數）。"""
    longest = max(len(a), len(b))
    return edit_distance(a, b) / longest if longest else None


def write_side_by_side(results: list[SttResult], path: Path, window: float = 30.0) -> Path:
    """逐段對照表 CSV（Excel 可開啟）：每 window 秒一列，各引擎在這段時間開始的發言並排。"""
    end = max((s.end for r in results for s in r.segments), default=0.0)
    rows = []
    t = min((s.start for r in results for s in r.segments), default=0.0)
    t = window * int(t // window)
    while t < end:
        cells = []
        for r in results:
            parts = [f"{s.speaker}：{s.text}" for s in r.segments if t <= s.start < t + window]
            cells.append("\n".join(parts))
        if any(cells):
            rows.append([format_time(t), *cells])
        t += window
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["時間", *[r.engine for r in results]])
        w.writerows(rows)
    return path


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _ratio(pair: tuple[int, int]) -> float | None:
    return pair[0] / pair[1] if pair[1] else None


def _mark(ok: bool | None) -> str:
    return "" if ok is None else (" ✔" if ok else " ✘")


def render_report(reports: list[EngineReport], thresholds: dict, audio_name: str, lang_audio: str | None,
                  side_by_side: str | None = None) -> str:
    """產出 Markdown 比較表。沒有標準答案時，正確率相關指標顯示「需要標準答案」。"""
    th = thresholds
    has_ref = all(r.has_reference for r in reports)
    need = "—（需要標準答案）"
    lines = ["# 第 0 階段 STT 引擎評估報告", ""]
    if not has_ref:
        lines += [("> **快速比較（沒有標準答案）**：比較處理時間、費用、講者數、詞庫詞彙與引擎之間的差異。"
                  "要計算字元錯誤率等正確率，請產生標準答案草稿並校正後再評估一次（可勾選「沿用先前的轉錄結果」，不會重複計費）。"), ""]
    lines += [
        f"- 測試錄音：{audio_name}",
        f"- 語言測試錄音：{lang_audio or '（未提供）'}",
    ]
    if side_by_side:
        lines.append(f"- 逐段對照表（各引擎文字並排，可用 Excel 開啟人工比對）：{side_by_side}")
    lines += [
        "",
        "| 指標 | 門檻 | " + " | ".join(r.engine for r in reports) + " |",
        "|---|---|" + "---|" * len(reports),
    ]

    def row(name: str, thr: str, cells: list[str]) -> None:
        lines.append(f"| {name} | {thr} | " + " | ".join(cells) + " |")

    def ref_row(name: str, thr: str, make: object) -> None:
        row(name, thr, [make(r) if r.has_reference else need for r in reports])  # type: ignore[operator]

    row("引擎版本", "", [r.version for r in reports])
    ref_row("字元錯誤率（CER）", f"≤ {th['cer_threshold'] * 100:.0f}%",
            lambda r: _pct(r.cer) + _mark(r.cer <= th["cer_threshold"] if r.cer is not None else None))
    ref_row("講者錯誤比例", f"≤ {th['speaker_error_threshold'] * 100:.0f}%",
            lambda r: _pct(r.speaker_error) + _mark(r.speaker_error <= th["speaker_error_threshold"]
                                                    if r.speaker_error is not None else None))

    def ratio_cell(pair: tuple[int, int], ok: object) -> str:
        v = _ratio(pair)
        return f"{_pct(v)}（{pair[0]}/{pair[1]}）" + _mark(ok(v) if v is not None else None)  # type: ignore[operator]

    ref_row("詞庫命中率", f"≥ {th['glossary_hit_threshold'] * 100:.0f}%",
            lambda r: ratio_cell(r.glossary, lambda v: v >= th["glossary_hit_threshold"]))
    row("詞庫詞彙出現次數", "越多越好", [str(r.glossary_found) for r in reports])
    ref_row("語言判定正確率", f"≥ {th['lang_accuracy_threshold'] * 100:.0f}%",
            lambda r: ratio_cell(r.lang_acc, lambda v: v >= th["lang_accuracy_threshold"]))
    ref_row("中文誤判率", f"≤ {th['zh_misjudge_threshold'] * 100:.0f}%",
            lambda r: ratio_cell(r.zh_misjudge, lambda v: v <= th["zh_misjudge_threshold"]))
    if len(reports) > 1:
        cells = []
        for r in reports:
            parts = []
            for o in reports:
                if o is not r:
                    d = difference_rate(r.norm_text, o.norm_text)
                    parts.append(f"與 {o.engine} {_pct(d)}")
            cells.append("；".join(parts))
        row("引擎之間的文字差異率", "供參考", cells)
    row("處理時間（實測）", "", [f"{r.elapsed_sec / 60:.1f} 分鐘（錄音 {r.audio_duration / 60:.1f} 分鐘）"
                                for r in reports])
    row("處理時間（換算 2 小時）", f"≤ {th['target_minutes_for_2h']} 分鐘",
        [f"{r.minutes_for_2h:.1f} 分鐘" + _mark(r.minutes_for_2h <= th["target_minutes_for_2h"])
         for r in reports])
    row("費用（換算 2 小時）", "供決定",
        [f"US$ {r.cost_for_2h:.2f}" if r.price_per_hour else "未設定單價" for r in reports])
    row("偵測到的講者數", "", [str(r.n_speakers) for r in reports])

    lines += ["", "## 備註", ""]
    if has_ref:
        lines.append("- CER 比較前，標準答案與辨識結果都先以規則刪除贅字並移除標點、空白。")
        lines.append("- 講者錯誤比例只計算標準答案中「只有一人發言」的時間；引擎講者依重疊時間對應到標準答案講者。")
    lines.append("- 詞庫詞彙出現次數：詞庫中的專有名詞在辨識結果中出現幾次；同一段錄音，次數多的引擎通常比較能辨識專有名詞。")
    if len(reports) > 1:
        lines.append("- 引擎之間的文字差異率：兩個引擎的結果（刪除贅字、標點後）有多少比例不同；差異大的地方請看逐段對照表，判斷哪個比較正確。")
    lines.append("- 處理時間依實測等比例換算，未計入網路上傳速度差異。")
    lines.append("- 費用依設定檔 price_per_hour 計算，請先依各供應商最新價目表填寫。")
    for r in reports:
        for note in r.notes:
            lines.append(f"- {r.engine}：{note}")
    lines += ["", "請依上表選定預設引擎，並在設定檔 [stt] default_engine 填入。", ""]
    return "\n".join(lines)