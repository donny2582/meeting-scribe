"""可由 CLI 或 GUI 呼叫的核心工作（轉錄、產生標準答案草稿、引擎評估）。

所有長時間工作都接受 ``progress(百分比, 狀態文字)`` 回呼；
``cancel()`` 回傳 True 時，會在下一次回報進度時以 :class:`Cancelled` 中止。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
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
) -> SttResult:
    """前處理＋語音轉文字。"""
    from meeting_scribe import usage

    report = _guard(progress, cancel)
    usage.check_budget(cfg)  # 本月費用已達上限時不開始
    ffmpeg = find_ffmpeg(cfg)
    work_dir.mkdir(parents=True, exist_ok=True)
    flac = work_dir / f"{audio.stem}.flac"
    if not flac.exists():
        report(0, f"音訊前處理中：{audio.name}")
        tmp = flac.with_suffix(".tmp.flac")
        preprocess_audio(audio, tmp, ffmpeg)
        tmp.replace(flac)
    report(0, "準備語音轉文字")
    engine = create_engine(engine_name, cfg, ffmpeg)
    entries = load_glossaries(glossaries)
    phrases = stt_phrases(entries, langs)
    report(0, f"{engine.display_name}：語言 {'、'.join(registry.languages[c].name for c in langs)}，"
              f"提示詞彙 {len(phrases)} 個")
    result = engine.transcribe(flac, registry.locales_for(langs), phrases, num_speakers, report, work_dir)
    usage.record_stt(usage.SERVICE_NAMES.get(engine_name, engine_name), result.engine_version,
                     result.audio_duration, engine.price_per_hour())
    if result.needs_zh_tw_localization:
        result = localize_if_possible(result, cfg, entries, report)
    return result


def localize_if_possible(result: SttResult, cfg: dict[str, Any], entries: list[Any],
                         report: ProgressCallback) -> SttResult:
    """有文字整理服務的金鑰時，立即由 AI 做臺灣繁體在地化；沒有金鑰或失敗時保留自動繁體轉換的結果。"""
    from meeting_scribe import usage
    from meeting_scribe.llm import LLM_PROVIDERS, LLMError, create_llm, provider_of

    label, key_name = LLM_PROVIDERS[provider_of(cfg)]
    if not get_secret(key_name):
        report(100, f"未設定 {label} 金鑰：中文只做自動繁體轉換，未做 AI 臺灣在地化")
        return result
    if usage.budget_status(cfg).exceeded:
        report(100, "本月費用已達上限：中文只做自動繁體轉換，未做 AI 臺灣在地化")
        return result
    from meeting_scribe.postprocess.localize import localize_result

    try:
        return localize_result(result, create_llm(cfg), cfg, entries, report)
    except LLMError as exc:
        log.error("臺灣在地化失敗：%s", exc)
        report(100, f"AI 臺灣在地化失敗（已保留自動繁體轉換的結果）：{exc}")
        return result


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
) -> TranscribeOutput:
    """轉錄並輸出到「輸出資料夾\\{日期時間}_{檔名}_{引擎}」。"""
    folder = out_root / f"{datetime.now():%Y%m%d_%H%M}_{audio.stem}_{engine_name}"
    result = transcribe_audio(engine_name, audio, langs, glossaries, cfg, registry, folder / "_work",
                              num_speakers, progress, cancel)
    json_path = folder / "stt_raw.json"
    json_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    txt_path = write_transcript_txt(result, folder / "逐字稿草稿.txt")
    log.info("轉錄完成：%d 個片段，%.1f 分鐘", len(result.segments), result.elapsed_sec / 60)
    return TranscribeOutput(folder, json_path, txt_path, result)


def make_reference(
    audio: Path, engine_name: str, langs: list[str], glossaries: list[Path], cfg: dict[str, Any],
    registry: LanguageRegistry, out_csv: Path, progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
) -> Path:
    """由引擎結果產生標準答案草稿 CSV。"""
    from meeting_scribe.benchmark import write_reference_draft

    work = out_csv.parent / "_work"
    work.mkdir(parents=True, exist_ok=True)
    result = load_or_transcribe(
        work / f"{audio.stem}_{engine_name}.json", True, progress,
        engine_name=engine_name, audio=audio, langs=langs, glossaries=glossaries,
        cfg=cfg, registry=registry, work_dir=work, cancel=cancel,
    )
    write_reference_draft(result.segments, out_csv, registry)
    return out_csv


@dataclass
class BenchmarkOutput:
    """引擎評估的輸出。"""

    report_path: Path | None
    markdown: str
    errors: dict[str, str] = field(default_factory=dict)


def run_benchmark(
    audio: Path, reference: Path, engines: list[str], langs: list[str], glossaries: list[Path],
    cfg: dict[str, Any], registry: LanguageRegistry, out_dir: Path, reuse: bool = False,
    lang_audio: Path | None = None, lang_reference: Path | None = None, lang_langs: list[str] | None = None,
    progress: ProgressCallback | None = None, cancel: CancelCheck | None = None,
) -> BenchmarkOutput:
    """第 0 階段引擎評估。單一引擎失敗時記錄錯誤並繼續評估其他引擎。"""
    from meeting_scribe.audio.preprocess import AudioError
    from meeting_scribe.benchmark import evaluate, load_reference, render_report
    from meeting_scribe.stt.base import SttError

    if lang_audio and not lang_reference:
        raise ValueError("提供日韓測試錄音時，也必須提供它的標準答案")
    work = out_dir / "_work"
    work.mkdir(parents=True, exist_ok=True)
    terms = [e.term for e in load_glossaries(glossaries)]
    ref = load_reference(reference)
    lang_ref = load_reference(lang_reference) if lang_reference else None
    reports = []
    errors: dict[str, str] = {}
    for i, name in enumerate(engines):
        def sub(pct: float, msg: str, i: int = i, name: str = name) -> None:
            if progress:
                progress((i + pct / 100) / len(engines) * 100, f"[{name}] {msg}")

        try:
            common = {"engine_name": name, "glossaries": glossaries, "cfg": cfg, "registry": registry,
                          "work_dir": work, "cancel": cancel}
            result = load_or_transcribe(work / f"{audio.stem}_{name}.json", reuse, sub,
                                        audio=audio, langs=langs, **common)
            lang_result = None
            if lang_audio:
                lang_result = load_or_transcribe(work / f"{lang_audio.stem}_{name}.json", reuse, sub,
                                                 audio=lang_audio, langs=lang_langs or langs, **common)
        except (SttError, AudioError) as exc:
            errors[name] = str(exc)
            log.error("引擎 %s 失敗：%s", name, exc)
            continue
        price = float(cfg.get("stt", {}).get(name, {}).get("price_per_hour", 0.0) or 0.0)
        reports.append(evaluate(result, ref, registry, terms, price, lang_result, lang_ref))

    if not reports:
        return BenchmarkOutput(None, "", errors)
    md = render_report(reports, cfg["benchmark"], audio.name, lang_audio.name if lang_audio else None)
    report_path = out_dir / "benchmark_report.md"
    report_path.write_text(md, encoding="utf-8")
    if progress:
        progress(100, "評估完成")
    return BenchmarkOutput(report_path, md, errors)
