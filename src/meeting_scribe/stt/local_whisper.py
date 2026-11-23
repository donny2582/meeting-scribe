"""本機 faster-whisper＋pyannote 引擎（選用，需要 NVIDIA 顯卡）。

目前只保留介面位置（SPEC D11）；有 GPU 環境時再實作。
"""

from __future__ import annotations

from pathlib import Path

from meeting_scribe.models import SttResult
from meeting_scribe.stt.base import ProgressCallback, SttEngine, SttError


def has_nvidia_gpu() -> bool:
    """偵測是否有可用的 NVIDIA 顯卡（以 nvidia-smi 是否存在判斷）。"""
    import shutil

    return shutil.which("nvidia-smi") is not None


class LocalWhisperEngine(SttEngine):
    """本機引擎（尚未實作）。"""

    name = "local"
    display_name = "本機 GPU（faster-whisper＋pyannote）"

    def version(self) -> str:
        return f"faster-whisper {self.engine_cfg.get('whisper_model', '')}"

    def transcribe(
        self,
        audio_path: Path,
        locales: list[str],
        phrases: list[str],
        num_speakers: int | None = None,
        progress: ProgressCallback | None = None,
        work_dir: Path | None = None,
    ) -> SttResult:
        raise SttError("本機 GPU 引擎尚未實作（需要 NVIDIA 顯卡與 pyannote 授權）")
