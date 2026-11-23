"""命令列介面。

子指令：
- ``init-config``：建立使用者設定檔
- ``set-key``／``show-keys``：設定或檢視 API 金鑰（存在 Windows 認證管理員）
- ``transcribe``：單一引擎轉錄，輸出 JSON
- ``make-reference``：由引擎結果產生標準答案草稿 CSV
- ``benchmark``：第 0 階段引擎評估，產出比較表
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from pathlib import Path

from meeting_scribe.audio.preprocess import AudioError, find_ffmpeg, preprocess_audio
from meeting_scribe.config import (
    SECRET_NAMES,
    assets_dir,
    get_secret,
    init_user_config,
    load_config,
    localappdata_dir,
    mask_secret,
    set_secret,
)
from meeting_scribe.models import SttResult
from meeting_scribe.postprocess.glossary import load_glossaries, stt_phrases
from meeting_scribe.postprocess.lang_detect import LanguageRegistry
from meeting_scribe.stt import ENGINE_NAMES, create_engine
from meeting_scribe.stt.base import SttError

log = logging.getLogger("meeting_scribe")


def setup_logging(verbose: bool = False) -> Path:
    """日誌寫入 %LOCALAPPDATA%\\MeetingScribe\\logs（不記錄逐字稿內容）。"""
    log_dir = localappdata_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "meeting_scribe.log"
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [fh, ch]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return path


def _print_progress(pct: float, msg: str) -> None:
    print(f"  [{pct:5.1f}%] {msg}", flush=True)


def _default_glossaries() -> list[Path]:
    return sorted((assets_dir() / "glossary").glob("*.csv"))


def _languages(args_langs: list[str] | None, cfg: dict, registry: LanguageRegistry) -> list[str]:
    langs = list(args_langs or cfg.get("languages", {}).get("default", ["zh", "en"]))
    base = registry.base_code
    if base not in langs:
        langs.insert(0, base)  # 中文固定勾選
    unknown = [c for c in langs if c not in registry.languages]
    if unknown:
        raise SystemExit(f"未知的語言代碼：{', '.join(unknown)}")
    return langs


def run_transcribe(
    engine_name: str, audio: Path, langs: list[str], glossaries: list[Path], cfg: dict,
    registry: LanguageRegistry, work_dir: Path, num_speakers: int | None = None,
) -> SttResult:
    """前處理＋轉錄（供 transcribe、make-reference、benchmark 共用）。"""
    ffmpeg = find_ffmpeg(cfg)
    flac = work_dir / f"{audio.stem}.flac"
    if not flac.exists():
        print(f"前處理：{audio.name}")
        preprocess_audio(audio, flac, ffmpeg)
    engine = create_engine(engine_name, cfg, ffmpeg)
    entries = load_glossaries(glossaries)
    phrases = stt_phrases(entries, langs)
    print(f"{engine.display_name}：語言 {','.join(langs)}，提示詞彙 {len(phrases)} 個")
    return engine.transcribe(flac, registry.locales_for(langs), phrases, num_speakers, _print_progress, work_dir)


def _load_or_transcribe(cache: Path, reuse: bool, **kwargs) -> SttResult:  # type: ignore[no-untyped-def]
    if reuse and cache.exists():
        print(f"沿用先前結果：{cache.name}")
        return SttResult.model_validate_json(cache.read_text(encoding="utf-8"))
    result = run_transcribe(**kwargs)
    cache.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result


def cmd_init_config(_args: argparse.Namespace) -> int:
    path = init_user_config()
    print(f"使用者設定檔：{path}")
    return 0


def cmd_set_key(args: argparse.Namespace) -> int:
    if args.name not in SECRET_NAMES:
        print(f"未知的金鑰名稱，可用：{', '.join(SECRET_NAMES)}")
        return 2
    value = getpass.getpass(f"請輸入{SECRET_NAMES[args.name]}（輸入時不會顯示）：").strip()
    if not value:
        print("未輸入，已取消。")
        return 1
    set_secret(args.name, value)
    print(f"已存入 Windows 認證管理員：{SECRET_NAMES[args.name]} {mask_secret(value)}")
    return 0


def cmd_show_keys(_args: argparse.Namespace) -> int:
    for name, label in SECRET_NAMES.items():
        print(f"{label}（{name}）：{mask_secret(get_secret(name))}")
    return 0


def cmd_transcribe(args: argparse.Namespace) -> int:
    cfg = load_config()
    registry = LanguageRegistry.load()
    langs = _languages(args.languages, cfg, registry)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    work = out.parent / "_work"
    work.mkdir(exist_ok=True)
    result = run_transcribe(args.engine, Path(args.audio), langs, _glossary_args(args), cfg, registry, work,
                            args.speakers)
    out.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    print(f"完成：{len(result.segments)} 個片段，耗時 {result.elapsed_sec / 60:.1f} 分鐘 → {out}")
    return 0


def cmd_make_reference(args: argparse.Namespace) -> int:
    from meeting_scribe.benchmark import write_reference_draft

    cfg = load_config()
    registry = LanguageRegistry.load()
    langs = _languages(args.languages, cfg, registry)
    out = Path(args.out)
    work = out.parent / "_work"
    work.mkdir(parents=True, exist_ok=True)
    result = _load_or_transcribe(
        work / f"{Path(args.audio).stem}_{args.engine}.json", True,
        engine_name=args.engine, audio=Path(args.audio), langs=langs, glossaries=_glossary_args(args),
        cfg=cfg, registry=registry, work_dir=work,
    )
    write_reference_draft(result.segments, out, registry)
    print(f"標準答案草稿：{out}\n請用 Excel 開啟，逐段校正 text、speaker、lang 後存檔（保留 CSV UTF-8 格式）。")
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    from meeting_scribe.benchmark import evaluate, load_reference, render_report

    cfg = load_config()
    registry = LanguageRegistry.load()
    langs = _languages(args.languages, cfg, registry)
    out_dir = Path(args.out_dir)
    work = out_dir / "_work"
    work.mkdir(parents=True, exist_ok=True)
    glossaries = _glossary_args(args)
    terms = [e.term for e in load_glossaries(glossaries)]
    ref = load_reference(Path(args.reference))
    lang_ref = load_reference(Path(args.lang_reference)) if args.lang_reference else None
    lang_langs = _languages(args.lang_languages, cfg, registry) if args.lang_audio else None
    if args.lang_audio and not args.lang_reference:
        raise SystemExit("提供 --lang-audio 時也必須提供 --lang-reference")

    reports = []
    for name in args.engines:
        print(f"\n=== 引擎：{name} ===")
        try:
            result = _load_or_transcribe(
                work / f"{Path(args.audio).stem}_{name}.json", args.reuse,
                engine_name=name, audio=Path(args.audio), langs=langs, glossaries=glossaries,
                cfg=cfg, registry=registry, work_dir=work,
            )
            lang_result = None
            if args.lang_audio and lang_langs:
                lang_result = _load_or_transcribe(
                    work / f"{Path(args.lang_audio).stem}_{name}.json", args.reuse,
                    engine_name=name, audio=Path(args.lang_audio), langs=lang_langs, glossaries=glossaries,
                    cfg=cfg, registry=registry, work_dir=work,
                )
        except (SttError, AudioError) as exc:
            print(f"引擎 {name} 失敗：{exc}")
            log.error("引擎 %s 失敗：%s", name, exc)
            continue
        price = float(cfg.get("stt", {}).get(name, {}).get("price_per_hour", 0.0) or 0.0)
        reports.append(evaluate(result, ref, registry, terms, price, lang_result, lang_ref))

    if not reports:
        print("沒有任何引擎完成評估。")
        return 1
    md = render_report(reports, cfg["benchmark"], Path(args.audio).name,
                       Path(args.lang_audio).name if args.lang_audio else None)
    report_path = out_dir / "benchmark_report.md"
    report_path.write_text(md, encoding="utf-8")
    print(f"\n比較表：{report_path}\n")
    print(md)
    return 0


def _glossary_args(args: argparse.Namespace) -> list[Path]:
    return [Path(p) for p in args.glossary] if args.glossary else _default_glossaries()


def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器。"""
    p = argparse.ArgumentParser(prog="meeting_scribe", description="會議紀錄產生器（MeetingScribe）命令列")
    p.add_argument("-v", "--verbose", action="store_true", help="顯示除錯訊息")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init-config", help="建立使用者設定檔")
    s.set_defaults(func=cmd_init_config)

    s = sub.add_parser("set-key", help="設定 API 金鑰（存在 Windows 認證管理員）")
    s.add_argument("name", choices=list(SECRET_NAMES))
    s.set_defaults(func=cmd_set_key)

    s = sub.add_parser("show-keys", help="檢視已設定的金鑰（只顯示末 4 碼）")
    s.set_defaults(func=cmd_show_keys)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--audio", required=True, help="錄音檔（mp3、m4a、wav、mp4）")
        sp.add_argument("--languages", nargs="+", help="會議語言代碼，例：zh en ja（預設依設定檔）")
        sp.add_argument("--glossary", nargs="+", help="詞庫 CSV（預設 assets\\glossary 下全部）")

    s = sub.add_parser("transcribe", help="以單一引擎轉錄，輸出 JSON")
    common(s)
    s.add_argument("--engine", choices=ENGINE_NAMES, required=True)
    s.add_argument("--speakers", type=int, help="預估講者人數")
    s.add_argument("--out", required=True, help="輸出 JSON 路徑")
    s.set_defaults(func=cmd_transcribe)

    s = sub.add_parser("make-reference", help="由引擎結果產生標準答案草稿 CSV")
    common(s)
    s.add_argument("--engine", choices=ENGINE_NAMES, default="azure")
    s.add_argument("--out", required=True, help="草稿 CSV 路徑")
    s.set_defaults(func=cmd_make_reference)

    s = sub.add_parser("benchmark", help="第 0 階段引擎評估")
    common(s)
    s.add_argument("--reference", required=True, help="標準答案 CSV")
    s.add_argument("--engines", nargs="+", choices=ENGINE_NAMES, default=["azure", "openai"])
    s.add_argument("--lang-audio", help="含日文或韓文發言的測試錄音（約 3 分鐘）")
    s.add_argument("--lang-reference", help="語言測試錄音的標準答案 CSV")
    s.add_argument("--lang-languages", nargs="+", default=["zh", "en", "ja", "ko"],
                   help="語言測試錄音使用的會議語言（預設 zh en ja ko）")
    s.add_argument("--out-dir", default="benchmark_out", help="輸出資料夾")
    s.add_argument("--reuse", action="store_true", help="沿用先前的轉錄結果，只重新計算指標")
    s.set_defaults(func=cmd_benchmark)
    return p


def main(argv: list[str] | None = None) -> int:
    """CLI 進入點。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        return int(args.func(args))
    except (SttError, AudioError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
