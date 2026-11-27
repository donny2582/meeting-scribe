r"""安裝後自我檢查（SPEC 12 第 5 階段驗收）：不需要 API 金鑰，在本機完成一次處理。

為什麼需要：打包成安裝檔時，很容易漏包檔案（例如少了 ffmpeg、OpenCC 字典或設定檔）。
在開發的電腦上能跑，換到沒裝 Python 的電腦可能就壞了，selftest 就是用來抓這種問題的。

檢查項目（共 9 項）：資源檔、設定、金鑰保存後端、繁簡轉換、ffmpeg 轉檔、多種音檔格式、
復原與還原原本內容、匯入逐字稿 → 刪除贅字 → 產出逐字稿 .docx 與心智圖 PNG／XMind、主視窗與播放元件。

共同規則：每個 ``_check_`` 函式通過時 ``return`` 一段說明文字；失敗時 ``raise RuntimeError``。
run_selftest 接住錯誤後標成「[失敗]」並繼續檢查下一項。

結果寫到 ``{輸出資料夾}\selftest_report.txt``，全部通過時結束代碼為 0。
沒有檢查雲端服務（轉錄、AI 校正、摘要），因為需要金鑰、網路，也要花錢。

用法：``MeetingScribe.exe selftest --out D:\檢查結果``（開發時 ``python -m meeting_scribe selftest``）。
"""

from __future__ import annotations

import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

# 假的逐字稿範例（3 段），故意放了贅字「呃」「嗯」「對對對」，用來測試刪贅字（第 8 項）。
SAMPLE_TRANSCRIPT = """00:00:05 講者1
呃，各位好，那我們開始今天的會議。
00:00:12 講者2
嗯，關於毒駕防制的部分，對對對，修法預計今年底完成。
00:00:25 講者1
好，那就請公路局下次會議前提送修正版。
"""

# 執行 ffmpeg 時不要跳出黑色命令視窗（Windows 的 CREATE_NO_WINDOW）。
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# ffmpeg 憑空產生 3 秒的 440Hz「嗶」聲（調音用的 La）與 3 秒藍色畫面，不必附真的錄音檔。
TONE = ["-f", "lavfi", "-i", "sine=frequency=440:duration=3"]
VIDEO = ["-f", "lavfi", "-i", "color=c=blue:s=160x120:d=3"]


def _run_ffmpeg(ffmpeg: Path, *args: str) -> None:
    """執行一次 ffmpeg（不顯示視窗、不詢問、覆寫既有檔案）；失敗時丟出 CalledProcessError。"""
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-loglevel", "error", *args],
        capture_output=True,
        check=True,
        creationflags=NO_WINDOW,
    )


# ── 9 項檢查 ───────────────────────────────────────────────────────


def _check_assets() -> str:
    """第 1 項｜資源檔：檢查程式需要的檔案都有包進來，回傳資源資料夾路徑。

    - default_config.toml（預設設定）、languages.toml（語言清單）、
      model_catalog.toml（模型清單）、term_map_tw.csv（臺灣用語對照）
    - glossary/seed_*.csv：至少要有一個詞庫範本（glob 用萬用字元找檔案，* 代表任意文字）。
    - summarize/prompts/common.txt：摘要用的 AI 提示詞。

    缺的檔案都收進 missing，最後一次列出，例如「缺少資源檔：languages.toml、glossary/seed_*.csv」。
    """
    from meeting_scribe import summarize
    from meeting_scribe.config import assets_dir

    root = assets_dir()
    need = ["default_config.toml", "languages.toml", "model_catalog.toml", "term_map_tw.csv"]
    missing = [n for n in need if not (root / n).is_file()]
    if not list((root / "glossary").glob("seed_*.csv")):
        missing.append("glossary/seed_*.csv")

    prompts = Path(summarize.__file__).parent / "prompts"
    if not (prompts / "common.txt").is_file():
        missing.append("summarize/prompts/common.txt")
    if missing:
        raise RuntimeError("缺少資源檔：" + "、".join(missing))
    return str(root)


def _check_config() -> str:
    """第 2 項｜設定檔：確認設定檔讀得出來、格式沒壞，回報「幾個區段、幾種 AI 工作」。

    TOML 寫錯（例如少了引號）時 load_config() 會出錯，這一項就會失敗。
    """
    from meeting_scribe.config import load_config
    from meeting_scribe.models_catalog import tasks

    cfg = load_config()
    return f"設定讀取正常（{len(cfg)} 個區段、{len(tasks())} 種 AI 工作）"


def _check_keyring() -> str:
    """第 3 項｜金鑰保存：確認 Windows 認證管理員能用（API 金鑰存在那裡），回傳後端名稱。

    keyring 找不到可用的保存位置時，會改用名稱含 fail 或 null 的「假後端」，存了也存不進去，
    所以名稱裡有這兩個字就判定失敗。
    """
    import keyring

    backend = keyring.get_keyring()
    name = type(backend).__module__ + "." + type(backend).__name__
    if "fail" in name.lower() or "null" in name.lower():
        raise RuntimeError(f"找不到可用的金鑰保存後端：{name}")
    return name


def _check_opencc() -> str:
    """第 4 項｜繁簡轉換：「这个软件的信息」必須一字不差轉成「這個軟體的資訊」。

    不只是簡轉繁，還檢查臺灣用語（軟件 → 軟體、信息 → 資訊）。
    OpenCC 字典檔沒包進來就會轉錯或出錯。
    """
    from meeting_scribe.postprocess.zh_tw import to_zh_tw

    out = to_zh_tw("这个软件的信息")
    if out != "這個軟體的資訊":
        raise RuntimeError(f"繁簡轉換結果不正確：{out}")
    return out


def _check_ffmpeg(work: Path) -> str:
    """第 5 項｜ffmpeg 音訊轉檔：確認 ffmpeg 找得到、能轉檔、「語音範圍」功能正常。

    1. 用 ffmpeg 產生 3 秒的嗶聲，存成 tone.wav。
    2. 用 preprocess_audio 轉成 FLAC，並指定 clip=(0.5, 2.5)，只取 0.5～2.5 秒。
    3. 檢查轉出來的長度是不是大約 2 秒（容許 1.5～2.5 秒）。
    """
    from meeting_scribe.audio.preprocess import find_ffmpeg, preprocess_audio

    ffmpeg = find_ffmpeg()
    wav = work / "tone.wav"
    _run_ffmpeg(ffmpeg, *TONE, str(wav))
    info = preprocess_audio(wav, work / "audio.flac", ffmpeg, clip=(0.5, 2.5))
    if not 1.5 <= info.duration <= 2.5:
        raise RuntimeError(f"轉檔後長度不正確：{info.duration:.2f} 秒")
    return f"{ffmpeg}（轉出 {info.duration:.1f} 秒 FLAC）"


def _check_formats(work: Path) -> str:
    """第 6 項｜多種音檔格式：常見錄音與影片格式都能轉檔；沒有聲音的檔案會在送出前擋下。

    1. cases 的 key 是副檔名、value 是「怎麼產生這種格式」的 ffmpeg 參數：
       aac、wma（Windows 錄音常見）、amr（舊手機錄音，只能 8000Hz）、ogg、mov（影片＋嗶聲）。
       每一種都先產生、再轉檔、再檢查長度。
    2. 產生一個只有畫面、沒有聲音的 mp4，這次預期 preprocess_audio 要丟出 AudioError；
       沒丟出反而是問題（無聲檔案會被送去雲端轉錄，白白花錢）。
       try／except／else 的 else＝「沒有發生錯誤時」才執行。
    """
    from meeting_scribe.audio.preprocess import AudioError, find_ffmpeg, preprocess_audio

    ffmpeg = find_ffmpeg()
    cases = {
        "aac": TONE + ["-c:a", "aac", "-f", "adts"],
        "wma": TONE + ["-c:a", "wmav2"],
        "amr": TONE + ["-ar", "8000", "-c:a", "libopencore_amrnb", "-b:a", "12.2k"],
        "ogg": TONE + ["-c:a", "libvorbis"],
        "mov": VIDEO + TONE + ["-c:a", "aac", "-shortest"],
    }
    passed = []
    for ext, args in cases.items():
        src = work / f"格式測試.{ext}"
        _run_ffmpeg(ffmpeg, *args, str(src))
        info = preprocess_audio(src, work / f"格式測試_{ext}.flac", ffmpeg, clip=(0.5, 2.5))
        if not 1.5 <= info.duration <= 2.5:
            raise RuntimeError(f".{ext} 轉檔後長度不正確：{info.duration:.2f} 秒")
        passed.append(ext)

    silent = work / "只有影像.mp4"
    _run_ffmpeg(ffmpeg, *VIDEO, "-c:v", "mpeg4", str(silent))
    try:
        preprocess_audio(silent, work / "silent.flac", ffmpeg)
    except AudioError:
        pass  # 有擋下 → 正確
    else:
        raise RuntimeError("沒有聲音的影片沒有被擋下")
    return "、".join(passed) + " 都能轉檔；沒有聲音的檔案會擋下"


def _check_undo(work: Path) -> str:
    """第 7 項｜復原與還原原本內容：逐字稿修改可以復原；還原原本內容在存檔、重新開啟後仍有效。

    1. 建立 3 段假逐字稿（講者1「第1段」、講者2「第2段」、講者3「第3段」），記下原本的樣子 original。
    2. 做 3 個操作，每次操作前先用 history.record 拍一張快照：
       講者3 改名成「公路局」→ 講者2 合併到講者1 → 第 1 段內容改成「改錯」。
    3. 按一次復原：回到「第1段」了嗎？
    4. 再按一次復原：講者2 回來了嗎？
    5. 存檔後重新開啟（save() → load()），再按「還原原本內容」：是否完全回到一開始、講者命名也清空？

    第 5 步特別重要：確認「原本內容」有存進檔案，關掉程式再打開仍能還原，不是只存在記憶體裡。
    """
    from meeting_scribe.models import RawSegment
    from meeting_scribe.transcript_io import EditHistory, TranscriptDocument

    segments = [
        RawSegment(start=i * 5, end=i * 5 + 3, speaker=f"講者{i + 1}", text=f"第{i + 1}段")
        for i in range(3)
    ]
    doc = TranscriptDocument(source="自我檢查", folder=str(work / "undo"), segments=segments)
    original = [(s.speaker, s.text) for s in doc.segments]

    history = EditHistory()
    history.record(doc, "講者命名")
    doc.rename_speaker("講者3", "公路局")
    history.record(doc, "合併講者")
    doc.merge_speaker("講者2", "講者1")
    history.record(doc, "修改內容")
    doc.segments[0] = doc.segments[0].model_copy(update={"text": "改錯"})

    if history.undo(doc) != "修改內容" or doc.segments[0].text != "第1段":
        raise RuntimeError("復原修改內容失敗")
    if history.undo(doc) != "合併講者" or doc.segments[1].speaker != "講者2":
        raise RuntimeError("復原合併講者失敗")

    reopened = TranscriptDocument.load(doc.save())
    restored = reopened.restore_original()
    same = [(s.speaker, s.text) for s in reopened.segments] == original
    if not restored or not same or reopened.speaker_names:
        raise RuntimeError("還原原本內容失敗")
    return "復原、還原原本內容（含存檔後重新開啟）正常"


def _check_documents(work: Path, out: Path) -> str:
    """第 8 項｜匯入 → 刪贅字 → 產出文件：用假資料跑一次完整流程，最接近真實使用情境。

    1. 把 SAMPLE_TRANSCRIPT 寫成 txt 並匯入，檢查是不是 3 段。
    2. 刪贅字：allow_ai=False＝只用規則層、不呼叫 AI（不需要金鑰、不花錢），
       檢查「呃」「對對對」是否都刪掉了。
    3. 產出逐字稿 .docx，再用 python-docx 讀回文字，確認「毒駕防制」在裡面（內容沒有遺失）。
    4. 產出 3 層的小心智圖（安裝自我檢查 → 毒駕防制 → 今年底完成修法），輸出 PNG 和 XMind。

    產出檔留在輸出資料夾 out（可以實際打開來看）；其他檔案放在暫存資料夾 work，檢查完自動刪除。
    """
    import docx

    from meeting_scribe import tasks
    from meeting_scribe.config import load_config
    from meeting_scribe.models import MindNode
    from meeting_scribe.postprocess.lang_detect import default_registry
    from meeting_scribe.render.docx_transcript import render_transcript
    from meeting_scribe.render.mindmap_png import render_mindmap_png
    from meeting_scribe.render.mindmap_xmind import render_mindmap_xmind
    from meeting_scribe.transcript_io import import_transcript

    # 1. 匯入
    src = work / "範例逐字稿.txt"
    src.write_text(SAMPLE_TRANSCRIPT, encoding="utf-8")
    doc = import_transcript(src, work / "job")
    if len(doc.segments) != 3:
        raise RuntimeError(f"匯入逐字稿片段數不正確：{len(doc.segments)}")
    doc.info.title = "安裝自我檢查"

    # 2. 刪贅字（只用規則層）
    cleaned, _report = tasks.clean_fillers(
        doc.segments,
        load_config(),
        default_registry(),
        checkpoint_file=work / "job" / "_work" / "filler.json",
        allow_ai=False,
    )
    if any(t in s.text for s in cleaned for t in ("呃", "對對對")):
        raise RuntimeError("刪除贅字沒有作用")

    # 3. 逐字稿 .docx
    clean_doc = doc.model_copy(update={"segments": cleaned})
    path = render_transcript(clean_doc, out)
    text = "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
    if "毒駕防制" not in text:
        raise RuntimeError("逐字稿 .docx 內容不完整")

    # 4. 心智圖
    root = MindNode(
        text="安裝自我檢查",
        children=[MindNode(text="毒駕防制", children=[MindNode(text="今年底完成修法")])],
    )
    png = render_mindmap_png(root, out / "心智圖_安裝自我檢查.png")
    xmind = render_mindmap_xmind(root, out / "心智圖_安裝自我檢查.xmind")
    return f"{path.name}、{png.name}、{xmind.name}"


def _check_gui() -> str:
    """第 9 項｜圖形介面：確認視窗能開，打包時沒漏掉 PySide6 的元件。

    1. ``QApplication.instance() or QApplication([])``：Qt 程式一定要先有一個 QApplication；
       已經有就沿用，沒有才新建（同時建立兩個會出錯）。
    2. 建立主視窗 MainWindow()，但不顯示。
    3. 確認「產出文件」分頁有「復原」和「還原原本內容」兩個按鈕（hasattr＝有沒有這個屬性）。
    4. 建立一個 QMediaPlayer（試聽語音範圍用的播放器），確認 QtMultimedia 有包進來，然後刪掉。
    5. 關閉視窗，processEvents() 讓 Qt 把關閉動作處理完。
    """
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtWidgets import QApplication

    from meeting_scribe.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    tabs = win.findChildren(type(win.centralWidget()))
    page = win.document
    for name in ("undo_button", "restore_button"):
        if not hasattr(page, name):
            raise RuntimeError(f"「產出文件」分頁缺少按鈕：{name}")

    player = QMediaPlayer()
    del player
    win.close()
    app.processEvents()
    return f"主視窗建立正常（{len(tabs)} 個元件）"


# ── 總指揮 ─────────────────────────────────────────────────────────


def run_selftest(out: Path, log: Callable[[str], None] = print) -> int:
    """依序執行全部 9 項檢查，結果寫到 out/selftest_report.txt，回傳失敗項目數。

    1. 建立輸出資料夾；報告開頭寫上時間，以及是「打包版」還是「開發版」
       （sys.frozen 只有 PyInstaller 打包後才會有）。
    2. tempfile.TemporaryDirectory 建立暫存資料夾給 work 用，with 區塊結束時自動整個刪掉。
    3. checks 清單每一項是「（名稱, 要執行的函式）」；需要參數的用 lambda 包起來
       （lambda＝現在先不執行、等一下再呼叫，這樣才能逐項計時、捕捉錯誤）。
    4. 逐項執行：perf_counter 計時；成功記「[通過]」；失敗記「[失敗]」並附 traceback（錯在哪一行）。
    5. 最後寫「全部通過」或「幾項失敗」。

    log：接收一行文字的函式，預設 print（印到畫面）；測試時可換成別的函式收集結果。
    """
    import tempfile

    out.mkdir(parents=True, exist_ok=True)
    edition = "打包版" if getattr(sys, "frozen", False) else "開發版"
    lines = [
        f"MeetingScribe 安裝自我檢查　{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"執行檔：{sys.executable}（{edition}）",
        "",
    ]
    failures = 0
    with tempfile.TemporaryDirectory(prefix="meetingscribe_selftest_") as tmp:
        work = Path(tmp)
        checks: list[tuple[str, Callable[[], str]]] = [
            ("資源檔", _check_assets),
            ("設定檔", _check_config),
            ("金鑰保存（Windows 認證管理員）", _check_keyring),
            ("繁簡轉換（OpenCC）", _check_opencc),
            ("ffmpeg 音訊轉檔", lambda: _check_ffmpeg(work)),
            ("多種音檔格式", lambda: _check_formats(work)),
            ("復原與還原原本內容", lambda: _check_undo(work)),
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
