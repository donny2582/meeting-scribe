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
    """轉錄並輸出到「輸出資料夾\\{日期時間}_{檔名}_{引擎}」。"""
    from meeting_scribe import checkpoint

    checkpoint.purge_old()
    folder = out_root / f"{datetime.now():%Y%m%d_%H%M}_{audio.stem}_{engine_name}"
    result = transcribe_audio(engine_name, audio, langs, glossaries, cfg, registry, folder / "_work",
                              num_speakers, progress, cancel, clip=clip)
    folder.mkdir(parents=True, exist_ok=True)
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
                          "work_dir": work, "cancel": cancel, "use_cache": False}  # 評估要重新量測處理時間
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
