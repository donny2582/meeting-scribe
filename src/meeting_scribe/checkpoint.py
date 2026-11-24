"""中斷續跑用的暫存（SPEC 3.2：每個階段都會存檔，可從中斷處繼續）。

位置：``%LOCALAPPDATA%\\MeetingScribe\\cache\\{錄音檔指紋}\\``

- ``audio.flac``：前處理後的音訊（重試時不必重新轉檔）
- ``{引擎}_{設定指紋}\\stt_result.json``：語音轉錄完成的結果。**一完成就存**，之後的 AI 處理失敗也不會遺失；
  同一個檔案、同一組設定再按「開始轉錄」時直接沿用，不會重複計費。
- ``{引擎}_{設定指紋}\\chunks\\``：分段轉錄時每一段的結果；中斷後從下一段繼續。
- ``{引擎}_{設定指紋}\\soniox_job.json``：Soniox 已送出的工作；伺服器已完成時直接取回，不重送音訊。
- ``{引擎}_{設定指紋}\\localize.json``：AI 臺灣在地化每一批的結果；中斷後只補做剩下的批次。

超過 30 天沒有使用的暫存會自動刪除。
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from meeting_scribe.config import localappdata_dir

log = logging.getLogger(__name__)

KEEP_DAYS = 30
_IGNORED_ENGINE_KEYS = {"price_per_hour"}  # 不影響轉錄結果的設定


def cache_root() -> Path:
    return localappdata_dir() / "cache"


def _digest(text: str, n: int = 16) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


def audio_dir(audio: Path, clip: tuple[float, float] | None = None) -> Path:
    """依錄音檔的完整路徑、大小、修改時間（以及選取的範圍）決定暫存資料夾。"""
    st = audio.stat()
    span = f"|{clip[0]:.2f}-{clip[1]:.2f}" if clip else ""
    return cache_root() / _digest(f"{audio.resolve()}|{st.st_size}|{st.st_mtime_ns}{span}")


def run_dir(audio: Path, engine: str, cfg: dict[str, Any], locales: list[str], phrases: list[str],
            num_speakers: int | None, clip: tuple[float, float] | None = None) -> Path:
    """同一個錄音檔（同一個範圍）＋同一組轉錄設定 → 同一個資料夾。"""
    engine_cfg = {k: v for k, v in cfg.get("stt", {}).get(engine, {}).items() if k not in _IGNORED_ENGINE_KEYS}
    key = json.dumps({"engine_cfg": engine_cfg, "locales": locales, "phrases": phrases,
                      "speakers": num_speakers}, sort_keys=True, ensure_ascii=False)
    return audio_dir(audio, clip) / f"{engine}_{_digest(key, 12)}"


def read_json(path: Path) -> Any:
    """讀取暫存 JSON；不存在或損毀時回傳 None。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path: Path, data: Any) -> None:
    """先寫暫存檔再改名，避免寫到一半斷電留下損毀的檔案。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def touch(path: Path) -> None:
    """更新資料夾的使用時間（自動清除用）。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        (path / ".last_used").write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def purge_old(days: int = KEEP_DAYS) -> int:
    """刪除超過 days 天沒用的暫存，回傳刪除的資料夾數。"""
    root = cache_root()
    if not root.exists():
        return 0
    limit = time.time() - days * 86400
    removed = 0
    for d in root.iterdir():
        marker = d / ".last_used"
        try:
            last = float(marker.read_text(encoding="utf-8")) if marker.exists() else d.stat().st_mtime
        except (OSError, ValueError):
            continue
        if last < limit:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    if removed:
        log.info("已清除 %d 個超過 %d 天的暫存", removed, days)
    return removed
