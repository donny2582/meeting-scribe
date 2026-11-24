"""安裝後自我檢查（SPEC 12 第 5 階段驗收）：不需要 API 金鑰，在本機完成一次處理。

檢查項目：資源檔、設定、金鑰保存後端、繁簡轉換、ffmpeg 轉檔、匯入逐字稿 → 刪除贅字 → 產出逐字稿 .docx、
心智圖 PNG／XMind、主視窗與播放元件。結果寫到 ``{輸出資料夾}\\selftest_report.txt``，全部通過時結束代碼為 0。

用法：``MeetingScribe.exe selftest --out D:\\檢查結果``（開發時 ``python -m meeting_scribe selftest``）。
"""

from __future__ import annotations

import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

SAMPLE_TRANSCRIPT = """00:00:05 講者1
呃，各位好，那我們開始今天的會議。
00:00:12 講者2
嗯，關於毒駕防制的部分，對對對，修法預計今年底完成。
00:00:25 講者1
好，那就請公路局下次會議前提送修正版。
"""


def _check_assets() -> str:
    from meeting_scribe.config import assets_dir

    root = assets_dir()
    need = ["default_config.toml", "languages.toml", "model_catalog.toml", "term_map_tw.csv"]
    missing = [n for n in need if not (root / n).is_file()]
    if not list((root / "glossary").glob("seed_*.csv")):
        missing.append("glossary/seed_*.csv")
    from meeting_scribe import summarize

    prompts = Path(summarize.__file__).parent / "prompts"
    if not (prompts / "common.txt").is_file():
        missing.append("summarize/prompts/common.txt")
    if missing:
        raise RuntimeError("缺少資源檔：" + "、".join(missing))
    return str(root)


def _check_config() -> str:
    from meeting_scribe.config import load_config
    from meeting_scribe.models_catalog import tasks

    cfg = load_config()
    return f"設定讀取正常（{len(cfg)} 個區段、{len(tasks())} 種 AI 工作）"


def _check_keyring() -> str:
    import keyring

    backend = keyring.get_keyring()
    name = type(backend).__module__ + "." + type(backend).__name__
    if "fail" in name.lower() or "null" in name.lower():
        raise RuntimeError(f"找不到可用的金鑰保存後端：{name}")
    return name


def _check_opencc() -> str:
    from meeting_scribe.postprocess.zh_tw import to_zh_tw

    out = to_zh_tw("这个软件的信息")
    if out != "這個軟體的資訊":
        raise RuntimeError(f"繁簡轉換結果不正確：{out}")
    return out


def _check_ffmpeg(work: Path) -> str:
    from meeting_scribe.audio.preprocess import find_ffmpeg, preprocess_audio

    ffmpeg = find_ffmpeg()
    wav = work / "tone.wav"
    subprocess.run([str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=3", str(wav)],
                   capture_output=True, check=True, creationflags=0x08000000 if sys.platform == "win32" else 0)
    info = preprocess_audio(wav, work / "audio.flac", ffmpeg, clip=(0.5, 2.5))
    if not 1.5 <= info.duration <= 2.5:
        raise RuntimeError(f"轉檔後長度不正確：{info.duration:.2f} 秒")
    return f"{ffmpeg}（轉出 {info.duration:.1f} 秒 FLAC）"


def _check_documents(work: Path, out: Path) -> str:
    import docx

    from meeting_scribe import tasks
    from meeting_scribe.config import load_config
    from meeting_scribe.models import MindNode
    from meeting_scribe.postprocess.lang_detect import default_registry
    from meeting_scribe.render.docx_transcript import render_transcript
    from meeting_scribe.render.mindmap_png import render_mindmap_png
    from meeting_scribe.render.mindmap_xmind import render_mindmap_xmind
    from meeting_scribe.transcript_io import import_transcript

    src = work / "範例逐字稿.txt"
    src.write_text(SAMPLE_TRANSCRIPT, encoding="utf-8")
    doc = import_transcript(src, work / "job")
    if len(doc.segments) != 3:
        raise RuntimeError(f"匯入逐字稿片段數不正確：{len(doc.segments)}")
    doc.info.title = "安裝自我檢查"
    cfg = load_config()
    cleaned, _rep = tasks.clean_fillers(doc.segments, cfg, default_registry(),
                                        checkpoint_file=work / "job" / "_work" / "filler.json", allow_ai=False)
    if any(t in s.text for s in cleaned for t in ("呃", "對對對")):
        raise RuntimeError("刪除贅字沒有作用")
    clean_doc = doc.model_copy(update={"segments": cleaned})
    path = render_transcript(clean_doc, out)
    text = "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
    if "毒駕防制" not in text:
        raise RuntimeError("逐字稿 .docx 內容不完整")
    root = MindNode(text="安裝自我檢查", children=[MindNode(text="毒駕防制", children=[MindNode(text="今年底完成修法")])])
    png = render_mindmap_png(root, out / "心智圖_安裝自我檢查.png")
    xmind = render_mindmap_xmind(root, out / "心智圖_安裝自我檢查.xmind")
    return f"{path.name}、{png.name}、{xmind.name}"


def _check_gui() -> str:
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtWidgets import QApplication

    from meeting_scribe.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    tabs = win.findChildren(type(win.centralWidget()))
    player = QMediaPlayer()
    del player
    win.close()
    app.processEvents()
    return f"主視窗建立正常（{len(tabs)} 個元件）"


def run_selftest(out: Path, log: Callable[[str], None] = print) -> int:
    """執行全部檢查，回傳失敗項目數。"""
    import tempfile

    out.mkdir(parents=True, exist_ok=True)
    lines = [f"MeetingScribe 安裝自我檢查　{datetime.now():%Y-%m-%d %H:%M:%S}",
             f"執行檔：{sys.executable}（{'打包版' if getattr(sys, 'frozen', False) else '開發版'}）", ""]
    failures = 0
    with tempfile.TemporaryDirectory(prefix="meetingscribe_selftest_") as tmp:
        work = Path(tmp)
        checks: list[tuple[str, Callable[[], str]]] = [
            ("資源檔", _check_assets),
            ("設定檔", _check_config),
            ("金鑰保存（Windows 認證管理員）", _check_keyring),
            ("繁簡轉換（OpenCC）", _check_opencc),
            ("ffmpeg 音訊轉檔", lambda: _check_ffmpeg(work)),
            ("匯入逐字稿 → 刪除贅字 → 產出文件", lambda: _check_documents(work, out)),
            ("圖形介面", _check_gui),
        ]
        for name, fn in checks:
            t0 = time.perf_counter()
            try:
                detail = fn()
                line = f"[通過] {name}：{detail}（{time.perf_counter() - t0:.1f} 秒）"
            except Exception as exc:  # noqa: BLE001 - 每一項都要回報，不中斷其他檢查
                failures += 1
                line = f"[失敗] {name}：{exc}\n{traceback.format_exc()}"
            lines.append(line)
            log(line)
    lines += ["", "結果：全部通過" if not failures else f"結果：{failures} 項失敗"]
    log(lines[-1])
    (out / "selftest_report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return failures
