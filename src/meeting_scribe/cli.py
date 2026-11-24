"""命令列介面（核心工作見 :mod:`meeting_scribe.tasks`）。

子指令：
- ``init-config``：建立使用者設定檔
- ``set-key``／``show-keys``：設定或檢視 API 金鑰（存在 Windows 認證管理員）
- ``transcribe``：單一引擎轉錄，輸出 JSON 與逐字稿草稿
- ``make-reference``：由引擎結果產生標準答案草稿 CSV
- ``benchmark``：第 0 階段引擎評估，產出比較表
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from pathlib import Path

from meeting_scribe import tasks
from meeting_scribe.audio.preprocess import AudioError
from meeting_scribe.config import (
    SECRET_NAMES,
    get_secret,
    init_user_config,
    load_config,
    localappdata_dir,
    mask_secret,
    set_secret,
)
from meeting_scribe.postprocess.lang_detect import LanguageRegistry
from meeting_scribe.stt import ENGINE_NAMES
from meeting_scribe.stt.base import SttError
from meeting_scribe.usage import BudgetExceeded

log = logging.getLogger("meeting_scribe")


def setup_logging(verbose: bool = False, console: bool = True) -> Path:
    """日誌寫入 %LOCALAPPDATA%\\MeetingScribe\\logs（不記錄逐字稿內容）。"""
    log_dir = localappdata_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "meeting_scribe.log"
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handlers: list[logging.Handler] = [fh]
    if console and sys.stderr is not None:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("%(message)s"))
        handlers.append(ch)
    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return path


def _print_progress(pct: float, msg: str) -> None:
    print(f"  [{pct:5.1f}%] {msg}", flush=True)


def _glossary_args(args: argparse.Namespace) -> list[Path]:
    return [Path(p) for p in args.glossary] if args.glossary else tasks.default_glossaries()


def _langs(codes: list[str] | None, cfg: dict, registry: LanguageRegistry) -> list[str]:
    try:
        return tasks.normalize_languages(codes, cfg, registry)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def cmd_init_config(_args: argparse.Namespace) -> int:
    print(f"使用者設定檔：{init_user_config()}")
    return 0


def cmd_set_key(args: argparse.Namespace) -> int:
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
    out = tasks.run_transcribe_job(
        args.engine, Path(args.audio), _langs(args.languages, cfg, registry), _glossary_args(args),
        cfg, registry, Path(args.out_dir), args.speakers, _print_progress,
    )
    print(f"完成：{len(out.result.segments)} 個片段，耗時 {out.result.elapsed_sec / 60:.1f} 分鐘 → {out.folder}")
    return 0


def cmd_make_reference(args: argparse.Namespace) -> int:
    cfg = load_config()
    registry = LanguageRegistry.load()
    out = tasks.make_reference(
        Path(args.audio), args.engine, _langs(args.languages, cfg, registry), _glossary_args(args),
        cfg, registry, Path(args.out), _print_progress,
    )
    print(f"標準答案草稿：{out}\n請用 Excel 開啟，逐段校正 text、speaker、lang 後存檔（保留 CSV UTF-8 格式）。")
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    cfg = load_config()
    registry = LanguageRegistry.load()
    try:
        out = tasks.run_benchmark(
            Path(args.audio), Path(args.reference), args.engines, _langs(args.languages, cfg, registry),
            _glossary_args(args), cfg, registry, Path(args.out_dir), args.reuse,
            Path(args.lang_audio) if args.lang_audio else None,
            Path(args.lang_reference) if args.lang_reference else None,
            _langs(args.lang_languages, cfg, registry), _print_progress,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    for name, err in out.errors.items():
        print(f"引擎 {name} 失敗：{err}")
    if out.report_path is None:
        print("沒有任何引擎完成評估。")
        return 1
    print(f"\n比較表：{out.report_path}\n\n{out.markdown}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器。"""
    p = argparse.ArgumentParser(prog="meeting_scribe", description="會議紀錄產生器（MeetingScribe）命令列")
    p.add_argument("-v", "--verbose", action="store_true", help="顯示除錯訊息")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init-config", help="建立使用者設定檔").set_defaults(func=cmd_init_config)
    s = sub.add_parser("set-key", help="設定 API 金鑰（存在 Windows 認證管理員）")
    s.add_argument("name", choices=list(SECRET_NAMES))
    s.set_defaults(func=cmd_set_key)
    sub.add_parser("show-keys", help="檢視已設定的金鑰（只顯示末 4 碼）").set_defaults(func=cmd_show_keys)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--audio", required=True, help="錄音檔（mp3、m4a、wav、mp4）")
        sp.add_argument("--languages", nargs="+", help="會議語言代碼，例：zh en ja（預設依設定檔）")
        sp.add_argument("--glossary", nargs="+", help="詞庫 CSV（預設使用者詞庫資料夾下全部）")

    s = sub.add_parser("transcribe", help="以單一引擎轉錄，輸出 JSON 與逐字稿草稿")
    common(s)
    s.add_argument("--engine", choices=ENGINE_NAMES, required=True)
    s.add_argument("--speakers", type=int, help="預估講者人數")
    s.add_argument("--out-dir", required=True, help="輸出資料夾")
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
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        return int(args.func(args))
    except (SttError, AudioError, tasks.Cancelled, BudgetExceeded) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
