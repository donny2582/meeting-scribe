r"""命令列介面（核心工作見 :mod:`meeting_scribe.tasks`）。

不開視窗、改用打字下指令操作程式。用途：

1. 測試與除錯：不必一直點視窗，一行指令就能重跑。
2. 符合開發守則：CLAUDE.md 要求「核心邏輯可用 CLI 獨立執行，GUI 只呼叫核心」。
   cli 和 GUI 背後都呼叫同一個 tasks.py，cli 能跑就代表核心沒問題。
3. 安裝後自我檢查：selftest 子指令就是從這裡執行的。

在 __main__.py 裡，有加參數的就交給 cli.main()，沒加參數的就開視窗。

使用方式（在專案資料夾的 PowerShell）::

    .\.venv\Scripts\python.exe -m meeting_scribe show-keys
    .\.venv\Scripts\python.exe -m meeting_scribe --help      # 列出全部子指令

子指令（共 7 個）：

- ``init-config``：建立使用者設定檔
- ``set-key``／``show-keys``：設定或檢視 API 金鑰（存在 Windows 認證管理員）
- ``transcribe``：單一引擎轉錄，輸出 JSON 與逐字稿草稿
- ``make-reference``：由引擎結果產生標準答案草稿 CSV
- ``benchmark``：第 0 階段引擎評估，產出比較表
- ``selftest``：安裝後自我檢查（不需金鑰）
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
    mask_secret,
    set_secret,
)
from meeting_scribe.postprocess.lang_detect import LanguageRegistry
from meeting_scribe.stt import ENGINE_NAMES
from meeting_scribe.stt.base import SttError
from meeting_scribe.usage import BudgetExceeded

log = logging.getLogger("meeting_scribe")


# ── 小工具（名稱前有底線＝這個檔案內部自己用） ──────────────────────────


def setup_logging(verbose: bool = False, console: bool = True) -> Path:
    r"""開啟日誌記錄，回傳日誌檔路徑。

    自己沒做事，直接轉給 diagnostics.setup_logging。
    日誌寫在 %LOCALAPPDATA%\MeetingScribe\logs（自動輪替、不記錄逐字稿內容）。
    verbose=True 時記得更詳細（除錯模式）。
    """
    from meeting_scribe import diagnostics

    return diagnostics.setup_logging(verbose, console)


def _print_progress(pct: float, msg: str) -> None:
    """把進度印到畫面上，例如「  [ 35.0%] 轉錄第 3／8 段」。

    tasks.py 做事時會不斷回報「做到幾 %、在做什麼」：
    GUI 收到後更新進度條，cli 收到後就用這個函式印成文字。
    5.1f＝總寬 5 個字元、小數 1 位（讓百分比對齊）；flush=True＝馬上印出，不等緩衝。
    """
    print(f"  [{pct:5.1f}%] {msg}", flush=True)


def _glossary_args(args: argparse.Namespace) -> list[Path]:
    """決定要用哪些詞庫檔。

    有用 --glossary a.csv b.csv 指定就用指定的；
    沒指定就用 tasks.default_glossaries()（詞庫資料夾裡的全部 CSV）。
    """
    return [Path(p) for p in args.glossary] if args.glossary else tasks.default_glossaries()


def _langs(codes: list[str] | None, cfg: dict, registry: LanguageRegistry) -> list[str]:
    """整理會議語言清單（例如 --languages zh en ja），交給 tasks.normalize_languages 檢查。

    打了不認識的語言代碼會出現 ValueError，這裡轉成 SystemExit：
    只印出錯誤訊息後結束程式，不會跳出一大串看不懂的錯誤堆疊。
    """
    try:
        return tasks.normalize_languages(codes, cfg, registry)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


# ── 子指令（每個 cmd_ 對應一個子指令；回傳 0＝成功、1＝失敗） ─────────────


def cmd_init_config(_args: argparse.Namespace) -> int:
    r"""子指令 init-config：建立使用者設定檔並印出位置。

    設定檔在 %APPDATA%\MeetingScribe\config.toml。
    參數叫 _args（前面有底線）：這個函式用不到參數，但 main() 一定會傳 args 進來，所以要收下。
    """
    print(f"使用者設定檔：{init_user_config()}")
    return 0


def cmd_set_key(args: argparse.Namespace) -> int:
    """子指令 set-key：設定 API 金鑰，例如 ``set-key soniox``。

    - getpass.getpass(...)：輸入金鑰時畫面不會顯示（跟輸入密碼一樣），避免旁人看到。
    - 沒輸入就印「已取消」，回傳 1。
    - 有輸入就用 set_secret 存進 Windows 認證管理員，並用 mask_secret 只顯示末 4 碼確認。
    """
    value = getpass.getpass(f"請輸入{SECRET_NAMES[args.name]}（輸入時不會顯示）：").strip()
    if not value:
        print("未輸入，已取消。")
        return 1
    set_secret(args.name, value)
    print(f"已存入 Windows 認證管理員：{SECRET_NAMES[args.name]} {mask_secret(value)}")
    return 0


def cmd_show_keys(_args: argparse.Namespace) -> int:
    """子指令 show-keys：列出每個服務的金鑰有沒有設定，只顯示末 4 碼。

    例如「Soniox API 金鑰（soniox）：****a1b2」。
    """
    for name, label in SECRET_NAMES.items():
        print(f"{label}（{name}）：{mask_secret(get_secret(name))}")
    return 0


def cmd_transcribe(args: argparse.Namespace) -> int:
    """子指令 transcribe：轉錄一個錄音檔。

    1. load_config() 讀設定；LanguageRegistry.load() 讀語言清單。
    2. 把引擎、錄音路徑、語言、詞庫、講者人數、輸出資料夾、印進度的函式，
       一起交給 tasks.run_transcribe_job，實際轉錄都在那裡做。
    3. 完成後印出片段數、耗時、存放資料夾。

    跟 GUI「轉錄」分頁按「開始轉錄」呼叫的是同一個函式，差別只在 GUI 把進度顯示成進度條。
    """
    cfg = load_config()
    registry = LanguageRegistry.load()
    out = tasks.run_transcribe_job(
        args.engine,
        Path(args.audio),
        _langs(args.languages, cfg, registry),
        _glossary_args(args),
        cfg,
        registry,
        Path(args.out_dir),
        args.speakers,
        _print_progress,
    )
    minutes = out.result.elapsed_sec / 60
    print(f"完成：{len(out.result.segments)} 個片段，耗時 {minutes:.1f} 分鐘 → {out.folder}")
    return 0


def cmd_make_reference(args: argparse.Namespace) -> int:
    """子指令 make-reference：產生引擎評估用的「標準答案草稿」CSV。

    先用一個引擎轉錄，把結果存成 CSV，讓使用者用 Excel 逐段修正成正確答案。
    實際工作在 tasks.make_reference。
    （引擎評估後來改成「不需要標準答案也能比較」，所以這個指令現在是選用的。）
    """
    cfg = load_config()
    registry = LanguageRegistry.load()
    out = tasks.make_reference(
        Path(args.audio),
        args.engine,
        _langs(args.languages, cfg, registry),
        _glossary_args(args),
        cfg,
        registry,
        Path(args.out),
        _print_progress,
    )
    print(f"標準答案草稿：{out}")
    print("請用 Excel 開啟，逐段校正 text、speaker、lang 後存檔（保留 CSV UTF-8 格式）。")
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    """子指令 benchmark：第 0 階段引擎評估，同一段錄音讓多個引擎都轉錄，產出比較表。

    - 參數很多，但都只是把使用者打的選項原封不動交給 tasks.run_benchmark。
    - ``Path(x) if x else None``：有給才轉成路徑，沒給就是 None（沒有）。
    - 跑完後，失敗的引擎逐一印出原因；一個都沒成功就回傳 1；有成功就印出比較表。
    """
    cfg = load_config()
    registry = LanguageRegistry.load()
    try:
        out = tasks.run_benchmark(
            Path(args.audio),
            Path(args.reference) if args.reference else None,
            args.engines,
            _langs(args.languages, cfg, registry),
            _glossary_args(args),
            cfg,
            registry,
            Path(args.out_dir),
            args.reuse,
            Path(args.lang_audio) if args.lang_audio else None,
            Path(args.lang_reference) if args.lang_reference else None,
            _langs(args.lang_languages, cfg, registry),
            _print_progress,
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


def cmd_selftest(args: argparse.Namespace) -> int:
    r"""子指令 selftest：安裝後的自我檢查（不需金鑰）。

    決定結果放哪裡（預設是「文件\MeetingScribe\自我檢查」），呼叫 run_selftest 跑 9 項檢查，
    有失敗就回傳 1。
    """
    from meeting_scribe.config import APP_NAME, documents_dir
    from meeting_scribe.selftest import run_selftest

    out = Path(args.out) if args.out else documents_dir() / APP_NAME / "自我檢查"
    failures = run_selftest(out)
    print(f"報告：{out / 'selftest_report.txt'}")
    return 1 if failures else 0


# ── 參數解析與進入點 ─────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器：定義有哪些子指令、每個子指令可以加哪些選項（Python 內建 argparse）。

    可以把它想成一份點餐單：

    - ``p = ArgumentParser(...)``：整張點餐單。
    - ``-v／--verbose``：每個指令都能加的選項（顯示除錯訊息）。
    - ``sub = add_subparsers(...)``：餐點分類，也就是子指令。
    - ``sub.add_parser("set-key", ...)``：新增一道餐點「set-key」。
    - ``s.add_argument("name", choices=...)``：這道餐點要選什麼（只能選清單裡的服務名稱）。
    - ``set_defaults(func=cmd_set_key)``：關鍵！「點這道餐點時，交給 cmd_set_key 處理」。

    裡面的 common(sp) 是函式中的函式：transcribe、make-reference、benchmark 都需要
    --audio、--languages、--glossary，寫成一個小函式共用，不必重複寫三次。

    required=True＝一定要給；default=...＝沒給時的預設值；nargs="+"＝可以給一個或多個值。
    argparse 會自動產生說明：``python -m meeting_scribe --help``。
    """
    p = argparse.ArgumentParser(prog="meeting_scribe", description="會議紀錄產生器（MeetingScribe）命令列")
    p.add_argument("-v", "--verbose", action="store_true", help="顯示除錯訊息")
    sub = p.add_subparsers(dest="command", required=True)

    # 設定與金鑰
    s = sub.add_parser("init-config", help="建立使用者設定檔")
    s.set_defaults(func=cmd_init_config)

    s = sub.add_parser("set-key", help="設定 API 金鑰（存在 Windows 認證管理員）")
    s.add_argument("name", choices=list(SECRET_NAMES))
    s.set_defaults(func=cmd_set_key)

    s = sub.add_parser("show-keys", help="檢視已設定的金鑰（只顯示末 4 碼）")
    s.set_defaults(func=cmd_show_keys)

    def common(sp: argparse.ArgumentParser) -> None:
        """加上 transcribe、make-reference、benchmark 共用的三個選項。"""
        sp.add_argument(
            "--audio",
            required=True,
            help="錄音檔（mp3、m4a、aac、wav、wma、flac、ogg、opus、amr、影片等，ffmpeg 能讀的格式都可以）",
        )
        sp.add_argument("--languages", nargs="+", help="會議語言代碼，例：zh en ja（預設依設定檔）")
        sp.add_argument("--glossary", nargs="+", help="詞庫 CSV（預設使用者詞庫資料夾下全部）")

    # 轉錄
    s = sub.add_parser("transcribe", help="以單一引擎轉錄，輸出 JSON 與逐字稿草稿")
    common(s)
    s.add_argument("--engine", choices=ENGINE_NAMES, required=True)
    s.add_argument("--speakers", type=int, help="預估講者人數")
    s.add_argument("--out-dir", required=True, help="輸出資料夾")
    s.set_defaults(func=cmd_transcribe)

    # 引擎評估
    s = sub.add_parser("make-reference", help="由引擎結果產生標準答案草稿 CSV")
    common(s)
    s.add_argument("--engine", choices=ENGINE_NAMES, default="azure")
    s.add_argument("--out", required=True, help="草稿 CSV 路徑")
    s.set_defaults(func=cmd_make_reference)

    s = sub.add_parser("benchmark", help="第 0 階段引擎評估")
    common(s)
    s.add_argument("--reference", help="標準答案 CSV（選填；沒有時只做快速比較，不計算正確率）")
    s.add_argument("--engines", nargs="+", choices=ENGINE_NAMES, default=["azure", "openai"])
    s.add_argument("--lang-audio", help="含日文或韓文發言的測試錄音（約 3 分鐘）")
    s.add_argument("--lang-reference", help="語言測試錄音的標準答案 CSV")
    s.add_argument(
        "--lang-languages",
        nargs="+",
        default=["zh", "en", "ja", "ko"],
        help="語言測試錄音使用的會議語言（預設 zh en ja ko）",
    )
    s.add_argument("--out-dir", default="benchmark_out", help="輸出資料夾")
    s.add_argument("--reuse", action="store_true", help="沿用先前的轉錄結果，只重新計算指標")
    s.set_defaults(func=cmd_benchmark)

    # 自我檢查
    s = sub.add_parser("selftest", help="安裝後自我檢查：不需金鑰，在本機完成一次處理")
    s.add_argument(
        "--out",
        default=None,
        help="檢查結果與產出檔案的資料夾（預設：文件\\MeetingScribe\\自我檢查）",
    )
    s.set_defaults(func=cmd_selftest)
    return p


def main(argv: list[str] | None = None) -> int:
    """CLI 進入點（__main__.py 有參數時呼叫這裡），依序做 4 件事。

    1. ``sys.stdout.reconfigure(encoding="utf-8")``：畫面輸出用 UTF-8，中文才不會變亂碼。
       先檢查 sys.stdout 不是 None，因為打包成無視窗的 exe 時可能沒有輸出畫面。
    2. ``build_parser().parse_args(argv)``：拿點餐單解讀使用者打的指令，得到 args。
    3. ``setup_logging(args.verbose)``：開日誌。
    4. ``args.func(args)``：呼叫 set_defaults 記下的那個 cmd_ 函式。

    外層 try／except：預期中的錯誤（轉錄失敗、音檔讀不到、使用者取消、超過每月費用上限）
    只印一行「錯誤：…」並回傳 1，不印出嚇人的程式錯誤堆疊。
    """
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        return int(args.func(args))
    except (SttError, AudioError, tasks.Cancelled, BudgetExceeded) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
