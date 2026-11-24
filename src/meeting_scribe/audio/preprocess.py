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

SUPPORTED_EXTENSIONS = {".mp3", ".m4a", ".wav", ".mp4", ".flac"}
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
    raise AudioError(
        "找不到 ffmpeg。請把 ffmpeg.exe 放到 assets\\ffmpeg\\，"
        "或執行 winget install Gyan.FFmpeg 後重新開啟終端機。"
    )


def _run(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW, check=False)


def probe_duration(path: Path, ffmpeg: Path) -> float:
    """以 ffmpeg 讀取音訊長度（秒）。"""
    if not path.is_file():
        raise AudioError(f"找不到檔案：{path}")
    proc = _run([str(ffmpeg), "-hide_banner", "-i", str(path)])
    m = _DURATION_RE.search(proc.stderr.decode("utf-8", errors="replace"))
    if not m:
        raise AudioError(f"無法讀取音訊長度，檔案可能已損毀或格式不支援：{path.name}")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def preprocess_audio(src: Path, dst: Path, ffmpeg: Path, normalize: bool = True,
                     clip: tuple[float, float] | None = None) -> AudioInfo:
    """把錄音檔轉成 16kHz 單聲道 FLAC，並做音量正規化。

    clip＝（起點秒, 終點秒）時只擷取這一段（「轉錄」分頁的語音範圍）。
    """
    if src.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise AudioError(f"不支援的檔案格式：{src.suffix}（支援 mp3、m4a、wav、mp4）")
    probe_duration(src, ffmpeg)  # 先檢查檔案可讀，損毀時不送雲端
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(ffmpeg), "-hide_banner", "-nostdin", "-y"]
    if clip:
        start, end = clip
        if end - start < 1:
            raise AudioError("選取的語音範圍太短（不到 1 秒）")
        cmd += ["-ss", f"{start:.2f}", "-t", f"{end - start:.2f}"]
    cmd += ["-i", str(src), "-vn", "-ac", "1", "-ar", "16000"]
    if normalize:
        cmd += ["-af", "loudnorm=I=-18:TP=-2:LRA=11"]
    cmd += ["-c:a", "flac", str(dst)]
    log.info("前處理開始：%s", src.suffix)
    proc = _run(cmd)
    if proc.returncode != 0 or not dst.is_file():
        tail = proc.stderr.decode("utf-8", errors="replace")[-500:]
        raise AudioError(f"ffmpeg 轉檔失敗：{tail}")
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
