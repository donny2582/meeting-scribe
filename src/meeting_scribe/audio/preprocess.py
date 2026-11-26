"""音訊前處理（SPEC 3.1 ①）：ffmpeg 轉 16kHz 單聲道、音量正規化、輸出 FLAC。"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meeting_scribe.config import assets_dir

log = logging.getLogger(__name__)

#: 常見的錄音格式（選檔視窗的篩選條件與說明用）。不在清單裡的副檔名也會交給 ffmpeg 判斷，有音軌就能轉錄。
AUDIO_EXTENSIONS = (
    ".mp3", ".m4a", ".aac", ".wav", ".wma", ".flac", ".ogg", ".oga", ".opus", ".amr", ".awb", ".3ga",
    ".aif", ".aiff", ".caf", ".ac3", ".m4b", ".mp2", ".mpga", ".weba", ".mka", ".ape", ".wv", ".au", ".spx",
)
#: 影片格式（只取音軌）
VIDEO_EXTENSIONS = (
    ".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".3gp", ".3g2", ".mpg", ".mpeg",
    ".ts", ".mts", ".m2ts",
)
SUPPORTED_EXTENSIONS = set(AUDIO_EXTENSIONS) | set(VIDEO_EXTENSIONS)
SUPPORTED_DESCRIPTION = ("mp3、m4a、aac、wav、wma、flac、ogg、opus、amr 等錄音檔，"
                         "以及 mp4、mov、mkv、avi 等影片（只取聲音）；其他格式也會嘗試讀取")
_AUDIO_STREAM_RE = re.compile(r"Stream #\d+:\d+.*?: Audio: ")
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class AudioError(Exception):
    """音訊檔無法處理（損毀、格式不支援、找不到 ffmpeg 等）。"""


@dataclass
class AudioInfo:
    """前處理結果。"""

    path: Path
    duration: float
    size_bytes: int


def find_ffmpeg(cfg: dict[str, Any] | None = None) -> Path:
    """尋找 ffmpeg：設定值 → assets\\ffmpeg\\ffmpeg.exe → 系統 PATH。"""
    configured = (cfg or {}).get("general", {}).get("ffmpeg_path", "")
    candidates = [Path(configured)] if configured else []
    candidates.append(assets_dir() / "ffmpeg" / "ffmpeg.exe")
    for c in candidates:
        if c.is_file():
            return c
    which = shutil.which("ffmpeg")
    if which:
        return Path(which)
    for found in _winget_locations():  # 剛用 winget 安裝、程式還沒重開時 PATH 尚未更新
        return found
    raise AudioError(
        "找不到 ffmpeg（處理錄音檔的免費工具：轉換格式、剪出語音範圍、切段，轉錄前一定要有）。\n\n"
        "安裝方式：在開始功能表搜尋「PowerShell」並開啟，輸入下面這行後按 Enter，等它顯示安裝完成：\n"
        "winget install Gyan.FFmpeg\n\n"
        "裝好後回到本程式再按一次即可，不需要重新開機。（正式版的安裝檔會內附，不必另外安裝。）"
    )


def _winget_locations() -> list[Path]:
    """winget 安裝 ffmpeg 的常見位置（使用者範圍）。"""
    import os

    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    base = Path(local) / "Microsoft" / "WinGet"
    found = [p for p in [base / "Links" / "ffmpeg.exe"] if p.is_file()]
    packages = base / "Packages"
    if packages.is_dir():
        found += sorted(packages.glob("Gyan.FFmpeg*/*/bin/ffmpeg.exe"), reverse=True)
    return found


def _run(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW, check=False)


def probe_duration(path: Path, ffmpeg: Path) -> float:
    """以 ffmpeg 讀取音訊長度（秒）；檔案沒有聲音、讀不到或長度為 0 時拋出 AudioError。"""
    if not path.is_file():
        raise AudioError(f"找不到檔案：{path}")
    proc = _run([str(ffmpeg), "-hide_banner", "-i", str(path)])
    info = proc.stderr.decode("utf-8", errors="replace")
    if "Input #0" not in info:
        raise AudioError(f"無法讀取「{path.name}」：檔案可能已損毀，或不是錄音檔（ffmpeg 無法辨識這個格式）。\n"
                         f"支援：{SUPPORTED_DESCRIPTION}。")
    if not _AUDIO_STREAM_RE.search(info):
        raise AudioError(f"「{path.name}」裡沒有聲音（只有影像或其他資料），無法轉錄。")
    m = _DURATION_RE.search(info)
    if m:
        h, mi, s = m.groups()
        duration = int(h) * 3600 + int(mi) * 60 + float(s)
    else:  # 有些格式（例如串流錄下的 aac）檔頭沒有長度：整段讀過一次來量測
        duration = _measure_by_decoding(path, ffmpeg)
    if duration <= 0:
        raise AudioError(f"「{path.name}」的長度是 0 秒，可能是空白或損毀的錄音檔。")
    return duration


_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def _measure_by_decoding(path: Path, ffmpeg: Path) -> float:
    """檔頭沒有長度資訊時，解碼整段音訊（不輸出檔案）取得長度。"""
    proc = _run([str(ffmpeg), "-hide_banner", "-nostdin", "-i", str(path), "-map", "0:a:0", "-f", "null", "-"])
    times = _TIME_RE.findall(proc.stderr.decode("utf-8", errors="replace"))
    if not times:
        raise AudioError(f"無法讀取「{path.name}」的長度，檔案可能已損毀。")
    h, mi, s = times[-1]
    return int(h) * 3600 + int(mi) * 60 + float(s)


def preprocess_audio(src: Path, dst: Path, ffmpeg: Path, normalize: bool = True,
                     clip: tuple[float, float] | None = None) -> AudioInfo:
    """把錄音檔轉成 16kHz 單聲道 FLAC，並做音量正規化。

    clip＝（起點秒, 終點秒）時只擷取這一段（「轉錄」分頁的語音範圍）。
    """
    # 不以副檔名限制：交給 ffmpeg 判斷（aac、wma、amr、影片…都可以），有音軌就能轉錄；損毀或沒有聲音時不送雲端
    probe_duration(src, ffmpeg)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(ffmpeg), "-hide_banner", "-nostdin", "-y"]
    if clip:
        start, end = clip
        if end - start < 1:
            raise AudioError("選取的語音範圍太短（不到 1 秒）")
        cmd += ["-ss", f"{start:.2f}", "-t", f"{end - start:.2f}"]
    cmd += ["-i", str(src), "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000"]  # 第一條音軌
    if normalize:
        cmd += ["-af", "loudnorm=I=-18:TP=-2:LRA=11"]
    cmd += ["-c:a", "flac", str(dst)]
    log.info("前處理開始：%s", src.suffix)
    proc = _run(cmd)
    if proc.returncode != 0 or not dst.is_file():
        tail = proc.stderr.decode("utf-8", errors="replace")[-500:]
        raise AudioError(f"「{src.name}」轉檔失敗（ffmpeg）：{tail}")
    return AudioInfo(dst, probe_duration(dst, ffmpeg), dst.stat().st_size)


def export_chunk(
    src: Path, dst: Path, ffmpeg: Path, start: float, duration: float, bitrate: str | None = None
) -> Path:
    """切出一段音訊；副檔名 .mp3 時以指定位元率壓縮，否則輸出 FLAC。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-y",
        "-ss", f"{start:.2f}", "-t", f"{duration:.2f}", "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000",
    ]
    if dst.suffix.lower() == ".mp3":
        cmd += ["-c:a", "libmp3lame", "-b:a", bitrate or "48k"]
    else:
        cmd += ["-c:a", "flac"]
    cmd.append(str(dst))
    proc = _run(cmd)
    if proc.returncode != 0 or not dst.is_file():
        tail = proc.stderr.decode("utf-8", errors="replace")[-500:]
        raise AudioError(f"切段失敗：{tail}")
    return dst
