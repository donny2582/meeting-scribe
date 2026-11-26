"""可由 CLI 或 GUI 呼叫的核心工作（轉錄、產生標準答案草稿、引擎評估）。

所有長時間工作都接受 ``progress(百分比, 狀態文字)`` 回呼；
``cancel()`` 回傳 True 時，會在下一次回報進度時以 :class:`Cancelled` 中止。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from meeting_scribe.audio.preprocess import find_ffmpeg, preprocess_audio
from meeting_scribe.config import ENGINE_KEYS, get_secret, user_glossary_dir
from meeting_scribe.models import SttResult
from meeting_scribe.postprocess.glossary import load_glossaries, stt_phrases
from meeting_scribe.postprocess.lang_detect import LanguageRegistry
from meeting_scribe.stt import create_engine
from meeting_scribe.stt.base import ProgressCallback

log = logging.getLogger(__name__)

CancelCheck = Callable[[], bool]


class Cancelled(Exception):
    """使用者取消工作。"""


def _guard(progress: ProgressCallback | None, cancel: CancelCheck | None) -> ProgressCallback:
    def report(pct: float, msg: str) -> None:
        if cancel and cancel():
            raise Cancelled("已取消")
        if progress:
            progress(pct, msg)

    return report


def default_glossaries() -> list[Path]:
    """使用者詞庫資料夾中的全部詞庫。"""
    return sorted(user_glossary_dir().glob("*.csv"))


def normalize_languages(codes: list[str] | None, cfg: dict[str, Any], registry: LanguageRegistry) -> list[str]:
    """補上固定勾選的中文，並檢查語言代碼。"""
    langs = list(codes or cfg.get("languages", {}).get("default", ["zh", "en"]))
    base = registry.base_code
    if base not in langs:
        langs.insert(0, base)
    unknown = [c for c in langs if c not in registry.languages]
    if unknown:
        raise ValueError(f"未知的語言代碼：{', '.join(unknown)}")
    return langs


def missing_keys(engines: list[str]) -> list[str]:
    """回傳所選引擎需要、但尚未設定的金鑰名稱。"""
    return [ENGINE_KEYS[e] for e in engines if e in ENGINE_KEYS and not get_secret(ENGINE_KEYS[e])]


def transcribe_audio(
    engine_name: str,
    audio: Path,
    langs: list[str],
    glossaries: list[Path],
    cfg: dict[str, Any],
    registry: LanguageRegistry,
    work_dir: Path,
    num_speakers: int | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
    use_cache: bool = True,
    clip: tuple[float, float] | None = None,
) -> SttResult:
    """前處理＋語音轉文字（＋需要時的 AI 臺灣在地化）。

    clip＝（起點秒, 終點秒）：只轉錄這一段；逐字稿時間碼會加上起點，對應原始錄音的時間（不是從 0 開始）。

    中斷續跑（use_cache=True）：前處理音訊、轉錄結果、分段結果、在地化批次都存在
    ``checkpoint`` 暫存；同一個檔案、同一組設定再執行時，已完成（已付費）的部分直接沿用。
    work_dir 只在 use_cache=False（引擎評估要重新量測）時使用。
    """
    from meeting_scribe import checkpoint, usage

    report = _guard(progress, cancel)
    usage.check_budget(cfg)  # 本月費用已達上限時不開始
    ffmpeg = find_ffmpeg(cfg)
    entries = load_glossaries(glossaries)
    phrases = stt_phrases(entries, langs)
    locales = registry.locales_for(langs)
    if use_cache:
        if not audio.is_file():
            from meeting_scribe.audio.preprocess import AudioError

            raise AudioError(f"找不到檔案：{audio}")
        base = checkpoint.audio_dir(audio, clip)
        checkpoint.touch(base)
        run = checkpoint.run_dir(audio, engine_name, cfg, locales, phrases, num_speakers, clip)
    else:
        base = run = work_dir
    base.mkdir(parents=True, exist_ok=True)
    run.mkdir(parents=True, exist_ok=True)
    flac = base / "audio.flac"
    if not flac.exists():
        span = f"（{format_hms(clip[0])}–{format_hms(clip[1])}）" if clip else ""
        report(0, f"音訊前處理中：{audio.name}{span}")
        tmp = flac.with_suffix(".tmp.flac")
        preprocess_audio(audio, tmp, ffmpeg, clip=clip)
        tmp.replace(flac)

    stt_file = run / "stt_result.json"
    engine = create_engine(engine_name, cfg, ffmpeg)
    if use_cache and stt_file.exists():
        result = SttResult.model_validate_json(stt_file.read_text(encoding="utf-8"))
        report(100, "沿用先前已完成的語音轉錄（不會重複計費）")
    else:
        report(0, f"{engine.display_name}：語言 {'、'.join(registry.languages[c].name for c in langs)}，"
                  f"提示詞彙 {len(phrases)} 個")
        result = engine.transcribe(flac, locales, phrases, num_speakers, report, run)
        if clip:
            result = shift_to_clip(result, clip)
        stt_file.write_text(result.model_dump_json(indent=1), encoding="utf-8")  # 轉錄一完成就存檔
        usage.record_stt(usage.SERVICE_NAMES.get(engine_name, engine_name), result.engine_version,
                         result.audio_duration, engine.price_per_hour())
    if result.needs_zh_tw_localization:
        result = localize_if_possible(result, cfg, entries, report, run / "localize.json")
    return result


def shift_to_clip(result: SttResult, clip: tuple[float, float]) -> SttResult:
    """片段時間加上範圍起點，讓逐字稿時間碼對應原始錄音。"""
    start, end = clip
    segments = [s.model_copy(update={"start": round(s.start + start, 2), "end": round(s.end + start, 2)})
                for s in result.segments]
    return result.model_copy(update={"segments": segments, "clip_start": start, "clip_end": end})


def localize_if_possible(result: SttResult, cfg: dict[str, Any], entries: list[Any],
                         report: ProgressCallback, checkpoint_file: Path | None = None) -> SttResult:
    """有文字整理服務的金鑰時，立即由 AI 做臺灣繁體在地化；沒有金鑰或失敗時保留自動繁體轉換的結果。

    checkpoint_file：每一批完成就存檔；中斷（例如餘額不足）時保留已完成的批次，下次只補做剩下的。
    """
    from meeting_scribe import usage
    from meeting_scribe.llm import LLM_PROVIDERS, LLMError, create_llm, provider_of

    label, key_name = LLM_PROVIDERS[provider_of(cfg)]
    if not get_secret(key_name):
        report(100, f"未設定 {label} 金鑰：中文只做自動繁體轉換，未做 AI 臺灣在地化")
        return result
    if usage.budget_status(cfg).exceeded:
        report(100, "本月費用已達上限：中文只做自動繁體轉換，未做 AI 臺灣在地化")
        return result
    from meeting_scribe.postprocess.localize import apply_checkpoint, localize_result

    try:
        return localize_result(result, create_llm(cfg), cfg, entries, report, checkpoint_file)
    except LLMError as exc:
        log.error("臺灣在地化中斷：%s", exc)
        partial, done, total = apply_checkpoint(result, checkpoint_file)
        if done:
            report(100, f"AI 臺灣在地化中斷（{exc}）：已完成 {done}/{total} 段並保存；補足餘額後，"
                        "對同一個錄音檔再按「開始轉錄」會從中斷處繼續，語音轉錄不會重複計費。")
        else:
            report(100, f"AI 臺灣在地化未完成（{exc}），已保留自動繁體轉換的結果；"
                        "之後對同一個錄音檔再按「開始轉錄」即可補做，語音轉錄不會重複計費。")
        return partial


def clean_fillers(
    segments: list[Any], cfg: dict[str, Any], registry: LanguageRegistry,
    progress: ProgressCallback | None = None, cancel: CancelCheck | None = None,
    checkpoint_file: Path | None = None, allow_ai: bool = True,
) -> tuple[list[Any], Any]:
    """產出文件前刪除贅字（SPEC 6.4）：規則層＋AI 層（有金鑰且未達費用上限時），只刪不改。

    回傳（清理後的片段，整段都是贅字的片段會移除；統計）。原始片段不會被修改。
    """
    from meeting_scribe import usage
    from meeting_scribe.llm import LLM_PROVIDERS, LLMError, create_llm, provider_of
    from meeting_scribe.postprocess.filler import clean_texts, remove_fillers_rule
    from meeting_scribe.postprocess.lang_detect import detect_language

    report_fn = _guard(progress, cancel)
    base = registry.base_code

    def rule(_sid: str, text: str) -> str:
        lang = detect_language(text, registry)
        rules = registry.languages.get(lang) or registry.languages[base]
        out = remove_fillers_rule(text, rules.fillers, rules.repeat_collapse)
        if lang not in (base, "mixed", "unknown"):  # 中文夾雜時也套用中文贅字
            return out
        zh = registry.languages[base]
        return remove_fillers_rule(out, zh.fillers, zh.repeat_collapse)

    llm = None
    label, key_name = LLM_PROVIDERS[provider_of(cfg)]
    if not allow_ai:
        report_fn(0, "只用規則刪除贅字")
    elif not get_secret(key_name):
        report_fn(0, f"未設定 {label} 金鑰：只用規則刪除贅字（呃、嗯、對對對…）")
    elif usage.budget_status(cfg).exceeded:
        report_fn(0, "本月費用已達上限：只用規則刪除贅字")
    else:
        try:
            llm = create_llm(cfg)
            report_fn(0, f"刪除贅字：先用規則，再由 AI（{label} {llm.model_for('filler')}）處理，只刪不改")
        except LLMError as exc:
            report_fn(0, f"無法使用 AI（{exc}）：只用規則刪除贅字")
    texts = [(f"S{i:05d}", s.text) for i, s in enumerate(segments)]
    max_chars = int(cfg.get("llm", {}).get("filler_batch_chars", 3000))
    cleaned, report = clean_texts(texts, rule, llm, max_chars, report_fn, checkpoint_file)
    kept = [i for i in range(len(segments)) if cleaned[f"S{i:05d}"].strip(" ，。、,.!?！？")]
    report.kept_indices = kept  # 對應原始片段位置（摘要依據編號用）
    out = [segments[i].model_copy(update={"text": cleaned[f"S{i:05d}"]}) for i in kept]
    return out, report


def suggest_corrections(
    doc: Any, cfg: dict[str, Any], progress: ProgressCallback | None = None, cancel: CancelCheck | None = None,
    want_roles: bool = True,
) -> Any:
    """AI 校正建議（語意與講者角色）：只產生建議，由使用者確認後再用 semantic.apply_suggestions 套用。

    - 詞庫「常見誤寫」一律檢查（不花費用）。
    - 有文字整理服務金鑰且未達費用上限時，再由 AI 做語意校正與講者角色推測；每批存檔，可續跑、不重複計費。
    """
    from meeting_scribe import usage
    from meeting_scribe.llm import LLM_PROVIDERS, LLMError, create_llm, provider_of
    from meeting_scribe.postprocess.localize import build_reference, load_term_map
    from meeting_scribe.postprocess.semantic import Suggestions, suggest_fixes, suggest_roles

    report = _guard(progress, cancel)
    entries = load_glossaries(default_glossaries())
    info = doc.info.model_dump()
    speakers = doc.speakers()
    names = [doc.display_name(s) for s in speakers]
    lines = [(i, format_hms(s.start) if doc.has_timecodes else "—", doc.display_name(s.speaker), s.text)
             for i, s in enumerate(doc.segments)]
    work = Path(doc.folder) / "_work" if doc.folder else None
    result = Suggestions()

    llm = None
    label, key_name = LLM_PROVIDERS[provider_of(cfg)]
    if not get_secret(key_name):
        result.notes.append(f"未設定 {label} 金鑰：只檢查詞庫的常見誤寫，沒有做 AI 語意校正與講者角色")
    elif usage.budget_status(cfg).exceeded:
        result.notes.append("本月費用已達上限：只檢查詞庫的常見誤寫")
    else:
        llm = create_llm(cfg)
        report(0, f"AI 校正：{label} {llm.model_for('correct')}，{len(doc.segments)} 段、{len(speakers)} 位講者")
    max_chars = int(cfg.get("llm", {}).get("correct_batch_chars", 2500))
    try:
        suggest_fixes(lines, info, names, entries, build_reference(load_term_map(), entries), llm, max_chars,
                      report, work / "semantic.json" if work else None, result)
        if llm is not None and want_roles:
            report(90, "AI 推測講者角色")
            code_lines = [(i, stamp, s.speaker, s.text) for (i, stamp, _n, _t), s in zip(lines, doc.segments,
                                                                                          strict=True)]
            suggest_roles(code_lines, info, speakers, set(doc.speaker_names), llm,
                          checkpoint_file=work / "roles.json" if work else None, result=result)
    except LLMError as exc:
        if not (result.fixes or result.roles):
            raise
        result.notes.append(f"AI 校正中斷（{exc}）：已完成的部分有存檔，再按一次會從中斷處繼續，不重複計費")
    report(100, f"AI 校正完成：{len(result.fixes)} 處文字建議、{len(result.roles)} 位講者建議"
                + (f"（{result.reused} 批沿用先前結果）" if result.reused else "")
                + (f"；{result.rejected} 筆修改幅度過大已略過" if result.rejected else ""))
    return result


def segment_id(index: int) -> str:
    """片段編號：S0001、S0002…（對應「產出文件」表格的列，從 1 起算）。"""
    return f"S{index + 1:04d}"


@dataclass
class MinutesOutput:
    """AI 整理會議紀錄的結果。"""

    summary: Any
    validation: Any
    json_path: Path
    minutes_path: Path | None = None  # 會議紀錄 .docx
    png_path: Path | None = None  # 心智圖 PNG
    xmind_path: Path | None = None  # 心智圖 XMind

    def files(self) -> list[Path]:
        return [p for p in (self.minutes_path, self.png_path, self.xmind_path) if p]


def summarize_meeting(
    doc: Any, pairs: list[tuple[int, Any]], cfg: dict[str, Any], out_dir: Path,
    progress: ProgressCallback | None = None, cancel: CancelCheck | None = None,
) -> MinutesOutput:
    """AI 整理討論事項、結論、待辦與心智圖（第 2 階段），並寫出會議紀錄草稿（文字預覽）與 summary JSON。

    pairs：（原始片段位置, 片段）— 片段文字可以是刪除贅字後的版本，編號仍對應原始位置。
    """
    from meeting_scribe import usage
    from meeting_scribe.llm import LLM_PROVIDERS, create_llm, provider_of
    from meeting_scribe.postprocess.localize import build_reference, load_term_map
    from meeting_scribe.summarize.summarizer import SummaryError, make_line, summarize
    from meeting_scribe.summarize.validator import validate_summary

    report = _guard(progress, cancel)
    label, key_name = LLM_PROVIDERS[provider_of(cfg)]
    if not get_secret(key_name):
        raise SummaryError(f"整理會議紀錄需要 AI：請先在「API 金鑰」分頁設定「文字整理處理」的 {label} 金鑰")
    status = usage.budget_status(cfg)
    if status.exceeded:
        raise SummaryError(f"本月估計費用 US$ {status.used:.2f} 已達上限，未進行 AI 整理")
    llm = create_llm(cfg)
    report(0, f"AI 整理會議紀錄：{label} {llm.model_for('summary')}，會議類型「{doc.info.meeting_type}」")

    lines = [make_line(segment_id(i), s.start, doc.display_name(s.speaker), s.text, s.speaker_uncertain)
             for i, s in pairs]
    speakers = list(dict.fromkeys(doc.display_name(s.speaker) for _, s in pairs))
    info = doc.info.model_dump()
    reference = build_reference(load_term_map(), load_glossaries(default_glossaries()))
    summary = summarize(lines, info, speakers, llm, cfg, reference, report, Path(doc.folder) / "_work")

    known = {segment_id(i) for i in range(len(doc.segments))}
    uncertain = {segment_id(i) for i, s in enumerate(doc.segments) if s.speaker_uncertain}
    validation = validate_summary(summary, known, uncertain)
    detail = "、".join(f"{k} {v} 條" for k, v in validation.reasons.items())
    report(100, f"依據驗證：共 {validation.items} 條，{validation.pending} 條標記（待查證）"
                + (f"（{detail}）" if detail else "") + (f"；移除 {validation.removed_ids} 個不存在的片段編號"
                                                       if validation.removed_ids else ""))

    json_path = Path(doc.folder) / f"summary_{doc.info.meeting_type}.json"
    json_path.write_text(summary.model_dump_json(indent=1), encoding="utf-8")
    out = MinutesOutput(summary, validation, json_path)
    out.minutes_path, out.png_path, out.xmind_path = render_outputs(doc, summary, cfg, out_dir, report)
    return out


def render_outputs(doc: Any, summary: Any, cfg: dict[str, Any], out_dir: Path,
                   progress: ProgressCallback | None = None) -> tuple[Path | None, Path | None, Path | None]:
    """依勾選項目產出會議紀錄 .docx、心智圖 PNG 與 XMind（第 3 階段）。不呼叫 AI。"""
    from meeting_scribe.render.docx_minutes import render_minutes
    from meeting_scribe.render.docx_transcript import available_path, output_filename
    from meeting_scribe.render.mindmap_png import render_mindmap_png
    from meeting_scribe.render.mindmap_xmind import render_mindmap_xmind

    def report(pct: float, msg: str) -> None:
        if progress:
            progress(pct, msg)

    info = doc.info
    times = {segment_id(i): (s.start, s.end, doc.display_name(s.speaker)) for i, s in enumerate(doc.segments)}
    minutes = png = xmind = None
    if info.want_discussion or info.want_conclusion or info.want_action_table:
        minutes = render_minutes(doc, summary, times, out_dir, cfg, report)
        report(100, f"會議紀錄 .docx 完成：{minutes.name}")
    if info.want_mindmap and summary.mindmap.text:
        meeting_date = date.fromisoformat(info.date)
        root = summary.mindmap.model_copy(update={"text": info.title.strip() or summary.mindmap.text})  # 保留兩行標題
        report(0, "心智圖：繪製 PNG（由左往右展開）")
        png = render_mindmap_png(root, available_path(out_dir / output_filename("心智圖", info.title, meeting_date,
                                                                                 ".png")))
        report(50, "心智圖：產生 XMind 檔")
        xmind = render_mindmap_xmind(root, available_path(out_dir / output_filename("心智圖", info.title,
                                                                                     meeting_date, ".xmind")))
        report(100, f"心智圖完成：{png.name}、{xmind.name}")
    return minutes, png, xmind


def load_or_transcribe(cache: Path, reuse: bool, progress: ProgressCallback | None = None,
                       **kwargs: Any) -> SttResult:
    """有快取且 reuse=True 時沿用，否則轉錄並寫入快取。"""
    if reuse and cache.exists():
        if progress:
            progress(100, f"沿用先前結果：{cache.name}")
        return SttResult.model_validate_json(cache.read_text(encoding="utf-8"))
    result = transcribe_audio(progress=progress, **kwargs)
    cache.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result


def format_hms(sec: float) -> str:
    """秒 → [時:分:秒]。"""
    s = int(sec)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def write_transcript_txt(result: SttResult, path: Path) -> Path:
    """把辨識結果寫成易讀的逐字稿草稿（未經後處理）。"""
    lines = [f"辨識引擎：{result.engine_version}", f"錄音長度：{format_hms(result.audio_duration)}"]
    if result.clip_end is not None:
        lines.append(f"轉錄範圍：{format_hms(result.clip_start)}–{format_hms(result.clip_end)}（時間碼對應原始錄音）")
    if result.localized_by and not result.needs_zh_tw_localization:
        lines.append(f"中文字形：已轉為臺灣繁體，並由 AI 完成臺灣在地化校潤（{result.localized_by}）")
    elif result.script_conversion:
        lines.append("中文字形：已自動轉為臺灣繁體；尚未完成 AI 臺灣在地化（需設定文字整理處理的 API 金鑰）")
    lines.append("")
    for s in result.segments:
        mark = "（疑似）" if s.speaker_uncertain else ""
        lines.append(f"[{format_hms(s.start)}] {s.speaker}{mark}：{s.text}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return path


@dataclass
class TranscribeOutput:
    """轉錄工作的輸出。"""

    folder: Path
    json_path: Path
    txt_path: Path
    result: SttResult


def run_transcribe_job(
    engine_name: str, audio: Path, langs: list[str], glossaries: list[Path], cfg: dict[str, Any],
    registry: LanguageRegistry, out_root: Path, num_speakers: int | None = None,
    progress: ProgressCallback | None = None, cancel: CancelCheck | None = None,
    clip: tuple[float, float] | None = None,
) -> TranscribeOutput:
    """轉錄，結果存在工作資料夾 ``%LOCALAPPDATA%\\MeetingScribe\\jobs\\{日期時間}_{檔名}``（程式用的檔案）。

    使用者看的檔案（逐字稿、會議紀錄、心智圖）在「產出文件」時才寫到
    ``輸出資料夾\\{民國日期}_{會議名稱}\\``（見 meeting_folder）。out_root 保留給相容用途。
    """
    from meeting_scribe import checkpoint
    from meeting_scribe.config import jobs_dir

    checkpoint.purge_old()
    folder = jobs_dir() / f"{datetime.now():%Y%m%d_%H%M}_{audio.stem}"
    result = transcribe_audio(engine_name, audio, langs, glossaries, cfg, registry, folder / "_work",
                              num_speakers, progress, cancel, clip=clip)
    folder.mkdir(parents=True, exist_ok=True)
    json_path = folder / "stt_raw.json"
    json_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    txt_path = write_transcript_txt(result, folder / "逐字稿草稿.txt")
    log.info("轉錄完成：%d 個片段，%.1f 分鐘", len(result.segments), result.elapsed_sec / 60)
    return TranscribeOutput(folder, json_path, txt_path, result)


def meeting_folder(out_root: Path, info: Any) -> Path:
    """每場會議一個輸出資料夾：``輸出資料夾\\{民國年月日}_{會議名稱第一行}``，只放產出的檔案。"""
    import re

    from meeting_scribe.render.roc_date import format_roc_ymd

    first = next((ln.strip() for ln in (info.title or "").splitlines() if ln.strip()), "") or "未命名會議"
    name = re.sub(r'[\\/:*?"<>|\t]', "", first)[:40].strip(" .")
    return out_root / f"{format_roc_ymd(date.fromisoformat(info.date))}_{name}"


#: 引擎評估資料夾中的標準答案檔名（步驟 1 產生草稿，校正後直接存回原檔，評估時自動使用）
REFERENCE_FILE = "reference_draft.csv"
LANG_REFERENCE_FILE = "reference_lang_draft.csv"
_REFERENCE_META = "reference_meta.json"


def _sha1(path: Path) -> str:
    import hashlib

    return hashlib.sha1(path.read_bytes()).hexdigest()


def reference_status(path: Path) -> str:
    """標準答案狀態：missing（尚未產生）、uncorrected（還是引擎產生的草稿）、corrected（已校正過）。"""
    from meeting_scribe import checkpoint

    if not path.exists():
        return "missing"
    meta = checkpoint.read_json(path.parent / _REFERENCE_META) or {}
    return "uncorrected" if meta.get(path.name) == _sha1(path) else "corrected"


def make_reference(
    audio: Path, engine_name: str, langs: list[str], glossaries: list[Path], cfg: dict[str, Any],
    registry: LanguageRegistry, out_csv: Path, progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
) -> Path:
    """由引擎結果產生標準答案草稿 CSV。

    已經校正過的同名檔案不會被覆蓋：先備份成「…_備份_日期時間.csv」再產生新草稿。
    """
    import shutil

    from meeting_scribe import checkpoint
    from meeting_scribe.benchmark import write_reference_draft

    work = out_csv.parent / "_work"
    work.mkdir(parents=True, exist_ok=True)
    result = load_or_transcribe(
        work / f"{audio.stem}_{engine_name}.json", True, progress,
        engine_name=engine_name, audio=audio, langs=langs, glossaries=glossaries,
        cfg=cfg, registry=registry, work_dir=work, cancel=cancel,
    )
    if reference_status(out_csv) == "corrected":
        backup = out_csv.with_name(f"{out_csv.stem}_備份_{datetime.now():%Y%m%d_%H%M%S}.csv")
        shutil.copyfile(out_csv, backup)
        if progress:
            progress(100, f"已校正的標準答案已備份為 {backup.name}")
    write_reference_draft(result.segments, out_csv, registry)
    meta_path = out_csv.parent / _REFERENCE_META
    meta = checkpoint.read_json(meta_path) or {}
    meta[out_csv.name] = _sha1(out_csv)  # 記下草稿指紋，用來判斷之後有沒有校正
    checkpoint.write_json(meta_path, meta)
    return out_csv


@dataclass
class BenchmarkOutput:
    """引擎評估的輸出。"""

    report_path: Path | None
    markdown: str
    errors: dict[str, str] = field(default_factory=dict)


def run_benchmark(
    audio: Path, reference: Path | None, engines: list[str], langs: list[str], glossaries: list[Path],
    cfg: dict[str, Any], registry: LanguageRegistry, out_dir: Path, reuse: bool = False,
    lang_audio: Path | None = None, lang_reference: Path | None = None, lang_langs: list[str] | None = None,
    progress: ProgressCallback | None = None, cancel: CancelCheck | None = None,
) -> BenchmarkOutput:
    """第 0 階段引擎評估。單一引擎失敗時記錄錯誤並繼續評估其他引擎。

    reference（標準答案）不存在或為 None 時做「快速比較」：不算正確率，只比較處理時間、費用、講者數、
    詞庫詞彙與引擎之間的差異。一律產生逐段對照表 engines_side_by_side.csv 供人工比對。
    """
    import shutil

    from meeting_scribe.audio.preprocess import AudioError
    from meeting_scribe.benchmark import evaluate, load_reference, render_report, write_side_by_side
    from meeting_scribe.stt.base import SttError

    if reference is not None and not reference.is_file():
        reference = None
    if lang_reference is not None and not lang_reference.is_file():
        lang_reference = None
    work = out_dir / "_work"
    work.mkdir(parents=True, exist_ok=True)
    terms = [e.term for e in load_glossaries(glossaries)]
    ref = load_reference(reference) if reference else None
    lang_ref = load_reference(lang_reference) if (lang_reference and ref is not None) else None
    if progress and ref is None:
        progress(0, "沒有標準答案：快速比較（處理時間、費用、講者數、詞庫詞彙、引擎之間的差異），不計算正確率")
    reports = []
    results = []
    errors: dict[str, str] = {}
    for i, name in enumerate(engines):
        def sub(pct: float, msg: str, i: int = i, name: str = name) -> None:
            if progress:
                progress((i + pct / 100) / len(engines) * 100, f"[{name}] {msg}")

        def run_dir(kind: str, name: str = name) -> Path:
            """每個錄音、每個引擎各用一個暫存資料夾（音訊與切段結果不可共用），重新評估前清空。"""
            d = work / f"{kind}_{name}"
            if not reuse:
                shutil.rmtree(d, ignore_errors=True)
            return d

        try:
            common = {"engine_name": name, "glossaries": glossaries, "cfg": cfg, "registry": registry,
                      "cancel": cancel, "use_cache": False}  # 評估要重新量測處理時間
            result = load_or_transcribe(work / f"{audio.stem}_{name}.json", reuse, sub,
                                        audio=audio, langs=langs, work_dir=run_dir("main"), **common)
            lang_result = None
            if lang_audio:
                lang_result = load_or_transcribe(work / f"{lang_audio.stem}_{name}.json", reuse, sub,
                                                 audio=lang_audio, langs=lang_langs or langs,
                                                 work_dir=run_dir("lang"), **common)
        except (SttError, AudioError) as exc:
            errors[name] = str(exc)
            log.error("引擎 %s 失敗：%s", name, exc)
            continue
        price = float(cfg.get("stt", {}).get(name, {}).get("price_per_hour", 0.0) or 0.0)
        results.append(result)
        reports.append(evaluate(result, ref, registry, terms, price, lang_result, lang_ref))

    if not reports:
        return BenchmarkOutput(None, "", errors)
    side = write_side_by_side(results, out_dir / "engines_side_by_side.csv")
    md = render_report(reports, cfg["benchmark"], audio.name, lang_audio.name if lang_audio else None, side.name)
    unchecked = [p.name for p in (reference, lang_reference) if p and reference_status(p) == "uncorrected"]
    if unchecked:
        md = (f"> ⚠ 標準答案（{'、'.join(unchecked)}）尚未校正，仍是引擎自動產生的草稿；"
              "產生草稿的引擎會顯得特別準，結果僅供參考。\n\n") + md
    report_path = out_dir / "benchmark_report.md"
    report_path.write_text(md, encoding="utf-8")
    if progress:
        progress(100, "評估完成")
    return BenchmarkOutput(report_path, md, errors)
