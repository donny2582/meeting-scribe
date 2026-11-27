"""可由 CLI 或 GUI 呼叫的核心工作（總機）：GUI 的按鈕和 cli 的指令都打到這裡，再分派給各工作站。

分成五組：

0. 共同機制：進度回報與取消（:class:`Cancelled`、``_guard``）
1. 準備工作：詞庫、語言、金鑰檢查
2. 轉錄：``run_transcribe_job`` → ``transcribe_audio`` → 語音範圍、AI 臺灣在地化、逐字稿草稿
3. 產出文件：刪除贅字、AI 校正建議、AI 整理會議紀錄、輸出 .docx／心智圖、會議資料夾
4. 引擎評估：標準答案草稿、多引擎比較

所有長時間工作都接受 ``progress(百分比, 狀態文字)`` 回呼；
``cancel()`` 回傳 True 時，會在下一次回報進度時以 :class:`Cancelled` 中止。

所有會花錢的步驟都遵守同一套模式：先檢查金鑰與費用上限 → 每批完成就存檔
→ 中斷時保留已完成的部分 → 再按一次從中斷處繼續、不重複計費。
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

#: 型別名稱：「一個不收參數、回傳 True/False 的函式」，用來問「使用者按了取消嗎？」。
CancelCheck = Callable[[], bool]


# ══ 0. 共同機制：進度回報與取消 ═══════════════════════════════════════


class Cancelled(Exception):
    """使用者取消工作。

    內容是空的，只用名字辨別；cli 的 main() 和 GUI 收到時顯示「已取消」，而不是當成程式出錯。
    """


def _guard(progress: ProgressCallback | None, cancel: CancelCheck | None) -> ProgressCallback:
    """把「回報進度」和「檢查取消」包成同一個函式 report 並回傳。

    report 每次被呼叫時：

    - 先問 cancel()：使用者按了取消嗎？是的話丟出 Cancelled，工作立刻停止。
    - 沒取消，就把進度交給 progress（GUI 更新進度條，cli 印一行文字）。

    好處：工作的程式只要寫 ``report(50, "轉錄中")``，就同時完成回報進度和檢查取消。
    這叫做「閉包」：內部函式 report 記得外面的 progress 和 cancel。
    """

    def report(pct: float, msg: str) -> None:
        """先檢查取消（取消就丟出 Cancelled），再回報進度。"""
        if cancel and cancel():
            raise Cancelled("已取消")
        if progress:
            progress(pct, msg)

    return report


# ══ 1. 準備工作 ════════════════════════════════════════════════════


def default_glossaries() -> list[Path]:
    """找出使用者詞庫資料夾裡的所有 CSV（依檔名排序）；使用者沒有指定詞庫時就用這些。"""
    return sorted(user_glossary_dir().glob("*.csv"))


def normalize_languages(codes: list[str] | None, cfg: dict[str, Any], registry: LanguageRegistry) -> list[str]:
    """整理會議語言清單：補上固定勾選的中文，並檢查語言代碼。

    - 有指定就用指定的；沒有就用設定檔的預設值，再沒有就用 ["zh", "en"]。
    - 中文（registry.base_code）一定要有，沒有就插到第一個；介面上中文固定勾選、不能取消，就是這裡保證的。
    - 有不認識的代碼就丟出 ValueError。
    """
    langs = list(codes or cfg.get("languages", {}).get("default", ["zh", "en"]))
    base = registry.base_code
    if base not in langs:
        langs.insert(0, base)
    unknown = [c for c in langs if c not in registry.languages]
    if unknown:
        raise ValueError(f"未知的語言代碼：{', '.join(unknown)}")
    return langs


def missing_keys(engines: list[str]) -> list[str]:
    """回傳所選引擎需要、但尚未設定的金鑰名稱。

    例如選了 Soniox 卻沒設定金鑰，就回傳 ["soniox_api_key"]。
    GUI 按下開始前會先呼叫，提醒使用者去「API 金鑰」分頁設定。
    """
    return [ENGINE_KEYS[e] for e in engines if e in ENGINE_KEYS and not get_secret(ENGINE_KEYS[e])]


# ══ 2. 轉錄（「轉錄」分頁的「開始轉錄」） ══════════════════════════════
#
#   run_transcribe_job          ← 按鈕呼叫這個
#    ├─ transcribe_audio        ← 真正做事
#    │   ├─ shift_to_clip       （有設定語音範圍時）
#    │   └─ localize_if_possible（需要臺灣在地化時）
#    └─ write_transcript_txt


@dataclass
class TranscribeOutput:
    """轉錄工作的輸出（包裹）：工作資料夾、stt_raw.json、逐字稿草稿.txt、轉錄結果。"""

    folder: Path
    json_path: Path
    txt_path: Path
    result: SttResult


def run_transcribe_job(
    engine_name: str,
    audio: Path,
    langs: list[str],
    glossaries: list[Path],
    cfg: dict[str, Any],
    registry: LanguageRegistry,
    out_root: Path,
    num_speakers: int | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
    clip: tuple[float, float] | None = None,
) -> TranscribeOutput:
    r"""轉錄的入口：轉錄完成後把結果存到工作資料夾 ``%LOCALAPPDATA%\MeetingScribe\jobs\{日期時間}_{檔名}``。

    1. checkpoint.purge_old()：清掉太舊的中斷續跑暫存，避免越積越多。
    2. 建立工作資料夾（程式用的檔案）。
    3. 呼叫 transcribe_audio 轉錄。
    4. 存兩個檔：stt_raw.json（完整資料，給程式讀）和 逐字稿草稿.txt（給人看）。
    5. 日誌只記「幾個片段、幾分鐘」，不記逐字稿內容（CLAUDE.md 規定）。

    使用者看的檔案（逐字稿、會議紀錄、心智圖）在「產出文件」時才寫到
    ``輸出資料夾\{民國日期}_{會議名稱}\``（見 meeting_folder）。

    out_root（輸出資料夾）已經沒有用到：從前轉錄結果直接放在輸出資料夾，
    後來改成只放工作資料夾，這個參數只是為了相容（GUI、cli、測試都還會傳）而保留。
    """
    from meeting_scribe import checkpoint
    from meeting_scribe.config import jobs_dir

    checkpoint.purge_old()
    folder = jobs_dir() / f"{datetime.now():%Y%m%d_%H%M}_{audio.stem}"
    result = transcribe_audio(
        engine_name, audio, langs, glossaries, cfg, registry, folder / "_work",
        num_speakers, progress, cancel, clip=clip,
    )
    folder.mkdir(parents=True, exist_ok=True)
    json_path = folder / "stt_raw.json"
    json_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    txt_path = write_transcript_txt(result, folder / "逐字稿草稿.txt")
    log.info("轉錄完成：%d 個片段，%.1f 分鐘", len(result.segments), result.elapsed_sec / 60)
    return TranscribeOutput(folder, json_path, txt_path, result)


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
    """音訊前處理 → 語音轉文字 →（需要時）AI 臺灣在地化。整個程式最核心的函式。

    1. 檢查費用上限：usage.check_budget(cfg)；本月已達上限就直接停止，不開始。
    2. 準備材料：找到 ffmpeg、讀詞庫、產生「提示詞彙」（stt_phrases，告訴轉錄引擎
       有哪些專有名詞，例如「路政及道安司」）、決定語言區域。
    3. 決定暫存位置（中斷續跑的關鍵）：

       - use_cache=True（一般轉錄）：依「錄音指紋＋引擎＋設定」算出固定的 ``checkpoint`` 暫存資料夾；
         同一個檔案、同一組設定每次都對應同一個資料夾，已完成（已付費）的部分直接沿用。
       - use_cache=False（引擎評估）：每次都重新做（要測量處理時間），使用 work_dir。

    4. 音訊前處理：還沒有 audio.flac 才轉檔；有 clip（語音範圍）就只擷取那一段。
       先存成 .tmp.flac、完成後才改名，轉到一半當機時只留下 .tmp 檔，下次不會誤以為已經轉好。
    5. 語音轉文字：已有 stt_result.json 就直接讀取（不重複計費）；沒有才呼叫引擎，
       完成馬上存檔，並用 usage.record_stt 記下用量和費用。
    6. 臺灣在地化：結果標記為需要在地化（Soniox 的中文）時，交給 localize_if_possible。

    clip＝（起點秒, 終點秒）：只轉錄這一段；逐字稿時間碼會加上起點，對應原始錄音（不是從 0 開始）。
    """
    from meeting_scribe import checkpoint, usage

    report = _guard(progress, cancel)

    # 1～2. 費用上限與材料
    usage.check_budget(cfg)  # 本月費用已達上限時不開始
    ffmpeg = find_ffmpeg(cfg)
    entries = load_glossaries(glossaries)
    phrases = stt_phrases(entries, langs)
    locales = registry.locales_for(langs)

    # 3. 暫存位置
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
    stt_file = run / "stt_result.json"
    log.debug(
        "轉錄暫存：音訊 %s（%s）、結果 %s（%s）",
        base, "已有" if flac.exists() else "沒有",
        run, "已有轉錄結果" if stt_file.exists() else "需要轉錄",
    )

    # 4. 音訊前處理
    if not flac.exists():
        span = f"（{format_hms(clip[0])}–{format_hms(clip[1])}）" if clip else ""
        report(0, f"音訊前處理中：{audio.name}{span}")
        tmp = flac.with_suffix(".tmp.flac")
        preprocess_audio(audio, tmp, ffmpeg, clip=clip)
        tmp.replace(flac)

    # 5. 語音轉文字
    engine = create_engine(engine_name, cfg, ffmpeg)
    if use_cache and stt_file.exists():
        result = SttResult.model_validate_json(stt_file.read_text(encoding="utf-8"))
        report(100, "沿用先前已完成的語音轉錄（不會重複計費）")
    else:
        lang_names = "、".join(registry.languages[c].name for c in langs)
        report(0, f"{engine.display_name}：語言 {lang_names}，提示詞彙 {len(phrases)} 個")
        result = engine.transcribe(flac, locales, phrases, num_speakers, report, run)
        if clip:
            result = shift_to_clip(result, clip)
        stt_file.write_text(result.model_dump_json(indent=1), encoding="utf-8")  # 轉錄一完成就存檔
        usage.record_stt(
            usage.SERVICE_NAMES.get(engine_name, engine_name),
            result.engine_version,
            result.audio_duration,
            engine.price_per_hour(),
        )

    # 6. 臺灣在地化
    if result.needs_zh_tw_localization:
        result = localize_if_possible(result, cfg, entries, report, run / "localize.json")
    return result


def shift_to_clip(result: SttResult, clip: tuple[float, float]) -> SttResult:
    """片段時間加上語音範圍的起點，讓逐字稿時間碼對應原始錄音。

    例如只轉錄 10:00～20:00，引擎回傳的時間從 0 開始算，「0:05」要改成「10:05」。
    model_copy(update=...) 是 pydantic 的寫法：複製一份並修改部分欄位，原本那份不動。
    """
    start, end = clip
    segments = [
        s.model_copy(update={"start": round(s.start + start, 2), "end": round(s.end + start, 2)})
        for s in result.segments
    ]
    return result.model_copy(update={"segments": segments, "clip_start": start, "clip_end": end})


def localize_if_possible(
    result: SttResult,
    cfg: dict[str, Any],
    entries: list[Any],
    report: ProgressCallback,
    checkpoint_file: Path | None = None,
) -> SttResult:
    """有文字整理服務的金鑰時，立即由 AI 做臺灣繁體在地化校潤；做不了也不會讓轉錄失敗。

    - 沒有金鑰，或本月已達費用上限 → 顯示訊息，回傳 OpenCC 自動繁體轉換的結果。
    - 有金鑰 → 呼叫 localize_result，每批完成就存檔（checkpoint_file，即 localize.json）。
    - 中途失敗（例如餘額不足）→ apply_checkpoint 把已完成的批次套用上去，並告訴使用者
      「已完成 X/Y 段，再按開始轉錄會從中斷處繼續」，下次只補做剩下的。
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


def write_transcript_txt(result: SttResult, path: Path) -> Path:
    """把轉錄結果寫成好讀的「逐字稿草稿.txt」（未經後處理），回傳檔案路徑。

    - 開頭寫：辨識引擎、錄音長度、轉錄範圍、中文字形狀態（有沒有完成 AI 在地化）。
    - 每段一行：``[00:01:23] 講者1：內容``；講者不確定時加「（疑似）」。
    - 用 utf-8-sig（有 BOM 的 UTF-8），用記事本或 Excel 打開才不會亂碼。
    """
    lines = [f"辨識引擎：{result.engine_version}", f"錄音長度：{format_hms(result.audio_duration)}"]
    if result.clip_end is not None:
        span = f"{format_hms(result.clip_start)}–{format_hms(result.clip_end)}"
        lines.append(f"轉錄範圍：{span}（時間碼對應原始錄音）")
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


def format_hms(sec: float) -> str:
    """秒數轉成「時:分:秒」，例如 3725 → "01:02:05"。

    ``//`` 是整數除法、``%`` 是取餘數；``:02d`` 表示不足兩位補 0。
    """
    s = int(sec)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


# ══ 3. 產出文件（「產出文件」分頁） ═════════════════════════════════════


def meeting_folder(out_root: Path, info: Any) -> Path:
    r"""每場會議一個輸出資料夾：``輸出資料夾\{民國年月日}_{會議名稱第一行}``，只放產出的檔案。

    - 會議名稱有多行時只取第一行；沒有名稱就用「未命名會議」。
    - ``re.sub(r'[\\/:*?"<>|\t]', "", ...)``：刪掉 Windows 檔名不能用的字元，例如 /、:、?。
    - ``[:40]``：最多 40 個字，避免路徑太長；``strip(" .")``：Windows 資料夾名稱不能用空白或句點結尾。
    """
    import re

    from meeting_scribe.render.roc_date import format_roc_ymd

    title_lines = (ln.strip() for ln in (info.title or "").splitlines())
    first = next((ln for ln in title_lines if ln), "") or "未命名會議"
    name = re.sub(r'[\\/:*?"<>|\t]', "", first)[:40].strip(" .")
    return out_root / f"{format_roc_ymd(date.fromisoformat(info.date))}_{name}"


def segment_id(index: int) -> str:
    """片段編號：第 0 段 → S0001（對應「產出文件」表格的列，從 1 起算）。

    AI 摘要的每一條都會標註依據哪幾段，例如「依據 S0012、S0015」。
    """
    return f"S{index + 1:04d}"


def clean_fillers(
    segments: list[Any],
    cfg: dict[str, Any],
    registry: LanguageRegistry,
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
    checkpoint_file: Path | None = None,
    allow_ai: bool = True,
) -> tuple[list[Any], Any]:
    """產出文件前刪除贅字（SPEC 6.4）：規則層＋AI 層（有金鑰且未達費用上限時），只刪不改。

    1. 內部函式 rule（規則層，不花錢）：先判斷這段是什麼語言，套用該語言的贅字清單；
       中文或中文夾雜其他語言（mixed）時，再套一次中文贅字清單。
    2. 決定要不要用 AI，依序檢查：allow_ai=False（自我檢查用）、沒有金鑰、已達費用上限、
       建立 AI 失敗；只要有一項成立就只用規則層。
    3. 每段加上編號 S00000、S00001…，交給 clean_texts 處理（逐批存檔、可續跑）。
    4. 整段都是贅字的片段會被移除（例如只說了「嗯。」，去掉標點後是空白）。
    5. kept_indices：記下保留了原本第幾段，讓後面的摘要能對應回原始位置。

    回傳（清理後的片段, 統計）。原始片段不會被修改，只影響輸出。
    """
    from meeting_scribe import usage
    from meeting_scribe.llm import LLM_PROVIDERS, LLMError, create_llm, provider_of
    from meeting_scribe.postprocess.filler import clean_texts, remove_fillers_rule
    from meeting_scribe.postprocess.lang_detect import detect_language

    report_fn = _guard(progress, cancel)
    base = registry.base_code

    # 1. 規則層
    def rule(_sid: str, text: str) -> str:
        """用規則刪除一段文字的贅字（依語言選清單；中文或夾雜中文時再套一次中文清單）。"""
        lang = detect_language(text, registry)
        rules = registry.languages.get(lang) or registry.languages[base]
        out = remove_fillers_rule(text, rules.fillers, rules.repeat_collapse)
        if lang not in (base, "mixed", "unknown"):  # 中文夾雜時也套用中文贅字
            return out
        zh = registry.languages[base]
        return remove_fillers_rule(out, zh.fillers, zh.repeat_collapse)

    # 2. 要不要用 AI
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

    # 3. 逐批處理
    texts = [(f"S{i:05d}", s.text) for i, s in enumerate(segments)]
    max_chars = int(cfg.get("llm", {}).get("filler_batch_chars", 3000))
    cleaned, report = clean_texts(texts, rule, llm, max_chars, report_fn, checkpoint_file)

    # 4～5. 移除整段都是贅字的片段，記下保留的原始位置
    kept = [i for i in range(len(segments)) if cleaned[f"S{i:05d}"].strip(" ，。、,.!?！？")]
    report.kept_indices = kept  # 對應原始片段位置（摘要依據編號用）
    out = [segments[i].model_copy(update={"text": cleaned[f"S{i:05d}"]}) for i in kept]
    return out, report


def suggest_corrections(
    doc: Any,
    cfg: dict[str, Any],
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
    want_roles: bool = True,
) -> Any:
    """「AI 校正」按鈕：只產生建議（語意與講者角色），使用者確認後才用 semantic.apply_suggestions 套用。

    1. 準備資料：會議資訊、講者名單、每段的（編號、時間碼、講者、內容）。
    2. 決定要不要用 AI：沒有金鑰或已達費用上限時，只做詞庫比對，並在 notes 記下原因。
    3. 三種檢查，各有自己的存檔、可續跑、不重複計費：

       - suggest_fixes：文字校正；詞庫「常見誤寫」一律檢查（不花錢），有 AI 時再做語意校正（semantic.json）。
       - suggest_roles：推測講者角色（例如講者2 是主席），並檢查同一人是否被拆成兩個講者（roles.json）。
       - suggest_reassignments：逐段檢查講者，找出標錯講者、一段混了兩個人（speaker_check.json）。

    4. 中途失敗：什麼建議都還沒產生 → 照常丟出錯誤；已有部分建議 → 保留，並提示再按一次會從中斷處繼續。
    5. 最後回報「X 處文字建議、Y 位講者角色、Z 項講者檢查」。

    細節：lines 的講者用顯示名稱（例如「公路局」），code_lines 用原始代碼（例如「講者2」）。
    推測角色時必須用代碼，AI 回答「講者2 是主席」時，程式才知道要改哪一個。
    """
    from meeting_scribe import usage
    from meeting_scribe.llm import LLM_PROVIDERS, LLMError, create_llm, provider_of
    from meeting_scribe.postprocess.localize import build_reference, load_term_map
    from meeting_scribe.postprocess.semantic import (
        Suggestions,
        suggest_fixes,
        suggest_reassignments,
        suggest_roles,
    )

    report = _guard(progress, cancel)
    llm_cfg = cfg.get("llm", {})

    # 1. 準備資料
    entries = load_glossaries(default_glossaries())
    info = doc.info.model_dump()
    speakers = doc.speakers()
    names = [doc.display_name(s) for s in speakers]
    lines = [
        (i, format_hms(s.start) if doc.has_timecodes else "—", doc.display_name(s.speaker), s.text)
        for i, s in enumerate(doc.segments)
    ]
    work = Path(doc.folder) / "_work" if doc.folder else None
    result = Suggestions()

    # 2. 要不要用 AI
    llm = None
    label, key_name = LLM_PROVIDERS[provider_of(cfg)]
    if not get_secret(key_name):
        result.notes.append(f"未設定 {label} 金鑰：只檢查詞庫的常見誤寫，沒有做 AI 語意校正與講者角色")
    elif usage.budget_status(cfg).exceeded:
        result.notes.append("本月費用已達上限：只檢查詞庫的常見誤寫")
    else:
        llm = create_llm(cfg)
        report(0, f"AI 校正：{label} {llm.model_for('correct')}，{len(doc.segments)} 段、{len(speakers)} 位講者")

    # 3. 三種檢查
    try:
        suggest_fixes(
            lines, info, names, entries, build_reference(load_term_map(), entries), llm,
            int(llm_cfg.get("correct_batch_chars", 2500)), report,
            work / "semantic.json" if work else None, result,
        )
        if llm is not None and want_roles:
            code_lines = [
                (i, stamp, s.speaker, s.text)
                for (i, stamp, _name, _text), s in zip(lines, doc.segments, strict=True)
            ]
            report(70, "AI 推測講者角色、檢查是否有同一人被拆成兩個講者")
            suggest_roles(
                code_lines, info, speakers, set(doc.speaker_names), llm,
                checkpoint_file=work / "roles.json" if work else None, result=result,
            )
            report(80, "AI 逐段檢查講者（標錯講者、一段混了兩個人）")
            suggest_reassignments(
                code_lines, info, speakers, {s: doc.display_name(s) for s in speakers}, llm,
                int(llm_cfg.get("speaker_check_chars", 12000)), report,
                work / "speaker_check.json" if work else None, result,
            )
    # 4. 中途失敗
    except LLMError as exc:
        if result.empty():
            raise
        result.notes.append(f"AI 校正中斷（{exc}）：已完成的部分有存檔，再按一次會從中斷處繼續，不重複計費")

    # 5. 回報
    summary = (f"AI 校正完成：{len(result.fixes)} 處文字建議、{len(result.roles)} 位講者角色、"
               f"{result.speaker_checks()} 項講者檢查")
    if result.reused:
        summary += f"（{result.reused} 批沿用先前結果）"
    if result.rejected:
        summary += f"；{result.rejected} 筆修改幅度過大已略過"
    report(100, summary)
    return result


@dataclass
class MinutesOutput:
    """AI 整理會議紀錄的結果（包裹）：摘要、依據驗證結果、summary json，以及實際產出的檔案。"""

    summary: Any
    validation: Any
    json_path: Path
    minutes_path: Path | None = None  # 會議紀錄 .docx
    png_path: Path | None = None  # 心智圖 PNG
    xmind_path: Path | None = None  # 心智圖 XMind

    def files(self) -> list[Path]:
        """只回傳實際有產生的檔案（例如沒勾心智圖就不會列出 PNG）。"""
        return [p for p in (self.minutes_path, self.png_path, self.xmind_path) if p]


def summarize_meeting(
    doc: Any,
    pairs: list[tuple[int, Any]],
    cfg: dict[str, Any],
    out_dir: Path,
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
) -> MinutesOutput:
    """AI 整理討論事項、結論、待辦與心智圖（第 2 階段），存成 summary JSON，再交給 render_outputs 產出檔案。

    1. 一定要有 AI：沒有金鑰或已達上限時直接丟出 SummaryError（摘要只靠規則做不出來）。
    2. 把每段做成一行給 AI 看（編號、時間、講者、內容、講者是否不確定）。
    3. 準備參考資料：臺灣用語對照表＋詞庫，讓 AI 用正確的名詞。
    4. summarize(...)：呼叫 AI，逐步存檔在 _work，可續跑。
    5. 依據驗證 validate_summary ★：檢查 AI 寫的每一條是否真的有依據——
       引用不存在的片段編號 → 移除；依據的片段講者不確定 → 標記「（待查證）」。
       這是為了防止 AI 捏造內容，公文裡的結論必須是會議上真的有講過的。
    6. 存成 summary_{會議類型}.json，再呼叫 render_outputs 產出 Word 和心智圖。

    pairs：（原始片段位置, 片段）。片段文字已經刪過贅字，但編號仍對應原始位置，
    這樣 S0012 才不會因為刪掉幾段而對錯。
    """
    from meeting_scribe import usage
    from meeting_scribe.llm import LLM_PROVIDERS, create_llm, provider_of
    from meeting_scribe.postprocess.localize import build_reference, load_term_map
    from meeting_scribe.summarize.summarizer import SummaryError, make_line, summarize
    from meeting_scribe.summarize.validator import validate_summary

    report = _guard(progress, cancel)

    # 1. 一定要有 AI
    label, key_name = LLM_PROVIDERS[provider_of(cfg)]
    if not get_secret(key_name):
        raise SummaryError(f"整理會議紀錄需要 AI：請先在「API 金鑰」分頁設定「文字整理處理」的 {label} 金鑰")
    status = usage.budget_status(cfg)
    if status.exceeded:
        raise SummaryError(f"本月估計費用 US$ {status.used:.2f} 已達上限，未進行 AI 整理")
    llm = create_llm(cfg)
    report(0, f"AI 整理會議紀錄：{label} {llm.model_for('summary')}，會議類型「{doc.info.meeting_type}」")

    # 2～4. 整理
    lines = [
        make_line(segment_id(i), s.start, doc.display_name(s.speaker), s.text, s.speaker_uncertain)
        for i, s in pairs
    ]
    speakers = list(dict.fromkeys(doc.display_name(s.speaker) for _, s in pairs))
    info = doc.info.model_dump()
    reference = build_reference(load_term_map(), load_glossaries(default_glossaries()))
    summary = summarize(lines, info, speakers, llm, cfg, reference, report, Path(doc.folder) / "_work")

    # 5. 依據驗證
    known = {segment_id(i) for i in range(len(doc.segments))}
    uncertain = {segment_id(i) for i, s in enumerate(doc.segments) if s.speaker_uncertain}
    validation = validate_summary(summary, known, uncertain)
    message = f"依據驗證：共 {validation.items} 條，{validation.pending} 條標記（待查證）"
    detail = "、".join(f"{k} {v} 條" for k, v in validation.reasons.items())
    if detail:
        message += f"（{detail}）"
    if validation.removed_ids:
        message += f"；移除 {validation.removed_ids} 個不存在的片段編號"
    report(100, message)

    # 6. 存檔並產出檔案
    json_path = Path(doc.folder) / f"summary_{doc.info.meeting_type}.json"
    json_path.write_text(summary.model_dump_json(indent=1), encoding="utf-8")
    out = MinutesOutput(summary, validation, json_path)
    out.minutes_path, out.png_path, out.xmind_path = render_outputs(doc, summary, cfg, out_dir, report)
    return out


def render_outputs(
    doc: Any,
    summary: Any,
    cfg: dict[str, Any],
    out_dir: Path,
    progress: ProgressCallback | None = None,
) -> tuple[Path | None, Path | None, Path | None]:
    """依勾選項目把摘要排版成檔案（第 3 階段），回傳（會議紀錄 .docx, 心智圖 PNG, XMind）。不呼叫 AI、不花錢。

    - 勾了討論事項、結論或待辦 → render_minutes 產生公文格式的會議紀錄 .docx。
    - 勾了心智圖 → 產生 PNG 和 XMind；中心節點用會議名稱（保留兩行標題）。
    - available_path：檔名已存在時自動加序號，不覆蓋舊檔。

    把「AI 整理」和「排版」分成兩個函式，改排版時不必重新付費呼叫 AI。
    """
    from meeting_scribe.render.docx_minutes import render_minutes
    from meeting_scribe.render.docx_transcript import available_path, output_filename
    from meeting_scribe.render.mindmap_png import render_mindmap_png
    from meeting_scribe.render.mindmap_xmind import render_mindmap_xmind

    def report(pct: float, msg: str) -> None:
        """回報進度（排版很快，不檢查取消）。"""
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

        def target(ext: str) -> Path:
            """心智圖的輸出檔名（已存在時自動加序號）。"""
            return available_path(out_dir / output_filename("心智圖", info.title, meeting_date, ext))

        report(0, "心智圖：繪製 PNG（由左往右展開）")
        png = render_mindmap_png(root, target(".png"))
        report(50, "心智圖：產生 XMind 檔")
        xmind = render_mindmap_xmind(root, target(".xmind"))
        report(100, f"心智圖完成：{png.name}、{xmind.name}")
    return minutes, png, xmind


# ══ 4. 引擎評估（「引擎評估」分頁，第 0 階段） ═══════════════════════════

#: 引擎評估資料夾中的標準答案檔名（步驟 1 產生草稿，校正後直接存回原檔，評估時自動使用）
REFERENCE_FILE = "reference_draft.csv"
LANG_REFERENCE_FILE = "reference_lang_draft.csv"
#: 記錄「草稿指紋」的檔案，用來判斷標準答案有沒有被校正過
_REFERENCE_META = "reference_meta.json"


@dataclass
class BenchmarkOutput:
    """引擎評估的輸出（包裹）：報告路徑、報告內容（Markdown）、失敗的引擎與原因。

    ``field(default_factory=dict)``：預設值是「一個新的空字典」；不能直接寫 ``= {}``，
    否則所有物件會共用同一個字典。
    """

    report_path: Path | None
    markdown: str
    errors: dict[str, str] = field(default_factory=dict)


def load_or_transcribe(
    cache: Path, reuse: bool, progress: ProgressCallback | None = None, **kwargs: Any
) -> SttResult:
    """有快取且 reuse=True 時沿用先前結果，否則轉錄並寫入快取。

    ``**kwargs``：其他參數全部收下，原封不動轉交給 transcribe_audio，所以不必一一列出那麼多參數。
    """
    if reuse and cache.exists():
        if progress:
            progress(100, f"沿用先前結果：{cache.name}")
        return SttResult.model_validate_json(cache.read_text(encoding="utf-8"))
    result = transcribe_audio(progress=progress, **kwargs)
    cache.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result


def _sha1(path: Path) -> str:
    """計算檔案的 SHA-1 指紋（檔案只要改一個字，指紋就會完全不同）。"""
    import hashlib

    return hashlib.sha1(path.read_bytes()).hexdigest()


def reference_status(path: Path) -> str:
    """標準答案狀態：missing（尚未產生）、uncorrected（還是引擎產生的草稿）、corrected（已校正過）。

    比對指紋：跟產生草稿時記下的一樣＝使用者還沒改過；不一樣＝已經用 Excel 校正過。
    不必問使用者「你改過了嗎」，比對指紋就知道。
    """
    from meeting_scribe import checkpoint

    if not path.exists():
        return "missing"
    meta = checkpoint.read_json(path.parent / _REFERENCE_META) or {}
    return "uncorrected" if meta.get(path.name) == _sha1(path) else "corrected"


def make_reference(
    audio: Path,
    engine_name: str,
    langs: list[str],
    glossaries: list[Path],
    cfg: dict[str, Any],
    registry: LanguageRegistry,
    out_csv: Path,
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
) -> Path:
    """由引擎結果產生標準答案草稿 CSV，回傳檔案路徑。

    1. 用一個引擎轉錄（有之前的結果就沿用）。
    2. 已經校正過的同名檔案不會被覆蓋：先備份成「…_備份_日期時間.csv」。
    3. 寫出新草稿，並記下它的指紋，供 reference_status 日後比對。
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


def run_benchmark(
    audio: Path,
    reference: Path | None,
    engines: list[str],
    langs: list[str],
    glossaries: list[Path],
    cfg: dict[str, Any],
    registry: LanguageRegistry,
    out_dir: Path,
    reuse: bool = False,
    lang_audio: Path | None = None,
    lang_reference: Path | None = None,
    lang_langs: list[str] | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
) -> BenchmarkOutput:
    """第 0 階段引擎評估：用同一段錄音讓多個引擎轉錄，產出比較表。

    1. 標準答案不存在或為 None 時做「快速比較」：不算正確率，只比較處理時間、費用、講者數、
       詞庫詞彙與引擎之間的差異。
    2. 對每個引擎：sub 把單一引擎的 0～100% 換算成整體進度；run_dir 讓每個錄音、每個引擎
       各用一個暫存資料夾；單一引擎失敗時記錄錯誤、繼續評估其他引擎；成功就用 evaluate 計算指標。
    3. 一律產生逐段對照表 engines_side_by_side.csv 供人工比對，以及報告 benchmark_report.md。
    4. 標準答案還沒校正時，報告最前面加上警告（產生草稿的引擎會顯得特別準）。

    Python 陷阱：迴圈裡定義的函式寫成 ``i: int = i``，是在定義當下把值存起來；
    直接使用 i 的話，會等到被呼叫時才讀，可能每個 sub 都讀到最後一個引擎。
    """
    import shutil

    from meeting_scribe.audio.preprocess import AudioError
    from meeting_scribe.benchmark import evaluate, load_reference, render_report, write_side_by_side
    from meeting_scribe.stt.base import SttError

    # 1. 標準答案
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

    # 2. 逐一評估每個引擎
    reports = []
    results = []
    errors: dict[str, str] = {}
    for i, name in enumerate(engines):

        def sub(pct: float, msg: str, i: int = i, name: str = name) -> None:
            """把單一引擎的進度換算成整體進度，訊息前加上 [引擎名稱]。"""
            if progress:
                progress((i + pct / 100) / len(engines) * 100, f"[{name}] {msg}")

        def run_dir(kind: str, name: str = name) -> Path:
            """每個錄音、每個引擎各用一個暫存資料夾（音訊與切段結果不可共用），重新評估前清空。"""
            d = work / f"{kind}_{name}"
            if not reuse:
                shutil.rmtree(d, ignore_errors=True)
            return d

        try:
            common = {
                "engine_name": name, "glossaries": glossaries, "cfg": cfg, "registry": registry,
                "cancel": cancel, "use_cache": False,  # 評估要重新量測處理時間
            }
            result = load_or_transcribe(
                work / f"{audio.stem}_{name}.json", reuse, sub,
                audio=audio, langs=langs, work_dir=run_dir("main"), **common,
            )
            lang_result = None
            if lang_audio:
                lang_result = load_or_transcribe(
                    work / f"{lang_audio.stem}_{name}.json", reuse, sub,
                    audio=lang_audio, langs=lang_langs or langs, work_dir=run_dir("lang"), **common,
                )
        except (SttError, AudioError) as exc:
            errors[name] = str(exc)
            log.error("引擎 %s 失敗：%s", name, exc)
            continue
        price = float(cfg.get("stt", {}).get(name, {}).get("price_per_hour", 0.0) or 0.0)
        results.append(result)
        reports.append(evaluate(result, ref, registry, terms, price, lang_result, lang_ref))

    if not reports:
        return BenchmarkOutput(None, "", errors)

    # 3. 對照表與報告
    side = write_side_by_side(results, out_dir / "engines_side_by_side.csv")
    md = render_report(reports, cfg["benchmark"], audio.name, lang_audio.name if lang_audio else None, side.name)

    # 4. 未校正的警告
    unchecked = [p.name for p in (reference, lang_reference) if p and reference_status(p) == "uncorrected"]
    if unchecked:
        md = (f"> ⚠ 標準答案（{'、'.join(unchecked)}）尚未校正，仍是引擎自動產生的草稿；"
              "產生草稿的引擎會顯得特別準，結果僅供參考。\n\n") + md
    report_path = out_dir / "benchmark_report.md"
    report_path.write_text(md, encoding="utf-8")
    if progress:
        progress(100, "評估完成")
    return BenchmarkOutput(report_path, md, errors)
