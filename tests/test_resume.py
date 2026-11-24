"""中斷續跑：轉錄結果、分段結果、AI 在地化批次都會保存，重試時不重複計費。"""

from pathlib import Path
from typing import Any

import pytest

from meeting_scribe import checkpoint, tasks
from meeting_scribe.config import load_config
from meeting_scribe.llm.base import LLMClient, LLMError
from meeting_scribe.models import RawSegment, SttResult
from meeting_scribe.postprocess.lang_detect import LanguageRegistry
from meeting_scribe.postprocess.localize import apply_checkpoint, localize_result
from meeting_scribe.stt.base import SttEngine, SttError


# ---- 分段轉錄 ----
class ChunkEngine(SttEngine):
    """每段回傳一個片段；fail_at 指定第幾段（從 0 起算）拋出餘額不足。"""

    name = "openai"

    def __init__(self, fail_at: int | None = None) -> None:
        super().__init__(load_config(), Path("ffmpeg"))
        self.fail_at = fail_at
        self.sent: list[int] = []
        self.sent_done: set[int] = set()

    def version(self) -> str:
        return "fake"

    def transcribe(self, *a: Any, **k: Any) -> SttResult:  # pragma: no cover - 不使用
        raise NotImplementedError

    def run(self, work: Path) -> list[RawSegment]:
        counter = iter(range(100))

        def transcribe_file(path: Path) -> tuple[list[RawSegment], dict]:
            idx = next(counter)
            while idx in self.sent_done:
                idx = next(counter)
            if idx == self.fail_at:
                raise SttError("OpenAI 請求過多或額度不足（HTTP 429）")
            self.sent.append(idx)
            return [RawSegment(start=600, end=601, speaker="A", text=f"第{idx}段")], {"n": idx}

        segs, _ = self._run_chunked(Path("a.flac"), 3000, 1200, 30, work, ".mp3", transcribe_file,
                                    lambda p, m: None, force_export=True, checkpoint_dir=work)
        return segs


def test_chunks_saved_and_resumed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("meeting_scribe.audio.preprocess.export_chunk",
                        lambda src, dst, ff, start, dur, bitrate=None: dst)
    first = ChunkEngine(fail_at=2)
    with pytest.raises(SttError, match="已完成並保存 2/3 段.*從第 3 段繼續"):
        first.run(tmp_path)
    assert first.sent == [0, 1]
    assert sorted(p.name for p in (tmp_path / "chunks").iterdir()) == ["chunk_000.json", "chunk_001.json"]

    second = ChunkEngine()
    second.sent_done = {0, 1}
    segs = second.run(tmp_path)
    assert second.sent == [2]  # 只送出第 3 段
    assert [s.text for s in segs] == ["第0段", "第1段", "第2段"]


# ---- 轉錄結果一完成就保存 ----
def test_stt_result_cached_and_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "會議.m4a"
    audio.write_bytes(b"x")
    calls: list[str] = []

    class FakeEngine:
        display_name = "假引擎"

        def transcribe(self, flac: Path, *a: Any, **k: Any) -> SttResult:
            calls.append("stt")
            return SttResult(engine="soniox", engine_version="v", audio_duration=60, elapsed_sec=1,
                             locales=["zh-TW"], segments=[RawSegment(start=0, end=1, speaker="講者1", text="好")])

        def price_per_hour(self) -> float:
            return 0.1

    monkeypatch.setattr(tasks, "find_ffmpeg", lambda cfg: Path("ffmpeg"))
    monkeypatch.setattr(tasks, "preprocess_audio", lambda src, dst, ff, **k: dst.write_bytes(b"flac"))
    monkeypatch.setattr(tasks, "create_engine", lambda name, cfg, ff: FakeEngine())
    args = ("soniox", audio, ["zh"], [], load_config(), LanguageRegistry.load(), tmp_path / "work")
    messages: list[str] = []
    tasks.transcribe_audio(*args)
    tasks.transcribe_audio(*args, progress=lambda p, m: messages.append(m))
    assert calls == ["stt"]  # 第二次沒有再呼叫引擎
    assert any("不會重複計費" in m for m in messages)
    assert (checkpoint.audio_dir(audio) / "audio.flac").exists()


def test_cache_key_changes_with_settings(tmp_path: Path) -> None:
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    cfg = load_config()
    a = checkpoint.run_dir(audio, "soniox", cfg, ["zh-TW"], [], None)
    cfg["stt"]["soniox"]["price_per_hour"] = 9.9  # 單價不影響結果
    assert checkpoint.run_dir(audio, "soniox", cfg, ["zh-TW"], [], None) == a
    assert checkpoint.run_dir(audio, "soniox", cfg, ["zh-TW", "ja-JP"], [], None) != a  # 換語言就重新轉錄
    audio.write_bytes(b"changed!")
    assert checkpoint.audio_dir(audio) != a.parent  # 檔案內容改了


# ---- AI 在地化每批保存 ----
class BatchLLM(LLMClient):
    name = "fake"

    def __init__(self, fail_on_call: int | None = None) -> None:
        super().__init__()
        self.models = {"localize": "m"}
        self.fail_on_call = fail_on_call
        self.calls = 0

    def complete_json(self, system: str, user: str, schema: dict, task: str, max_tokens: int = 16000) -> dict:
        import json

        self.calls += 1
        if self.calls == self.fail_on_call:
            raise LLMError("Anthropic 請求過多或額度不足，請稍後再試", fatal=True)
        items = json.loads(user.split("\n", 1)[1])["items"]
        return {"items": [{"id": i["id"], "text": i["text"].replace("項目", "專案")} for i in items]}


def _result(n: int = 4) -> SttResult:
    segs = [RawSegment(start=i, end=i + 1, speaker="講者1", text=f"第{i}個項目討論" * 10, lang="zh") for i in range(n)]
    return SttResult(engine="soniox", engine_version="v", audio_duration=n, elapsed_sec=1, locales=["zh-TW"],
                     segments=segs, needs_zh_tw_localization=True)


def test_localize_saves_each_batch_and_resumes(tmp_path: Path) -> None:
    cfg = load_config()
    cfg["llm"]["localize_batch_chars"] = 70  # 每批 1 段
    ckpt = tmp_path / "localize.json"
    with pytest.raises(LLMError):
        localize_result(_result(), BatchLLM(fail_on_call=3), cfg, checkpoint_file=ckpt)
    partial, done, total = apply_checkpoint(_result(), ckpt)
    assert (done, total) == (2, 4)  # 前 2 批已保存
    assert "專案" in partial.segments[1].text and "項目" in partial.segments[2].text
    assert partial.needs_zh_tw_localization

    llm = BatchLLM()
    out = localize_result(_result(), llm, cfg, checkpoint_file=ckpt)
    assert llm.calls == 2  # 只補做剩下的 2 批
    assert all("專案" in s.text for s in out.segments) and not out.needs_zh_tw_localization


def test_localize_if_possible_keeps_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config()
    cfg["llm"]["localize_batch_chars"] = 70
    monkeypatch.setattr(tasks, "get_secret", lambda n: "sk-ant-test")
    monkeypatch.setattr("meeting_scribe.llm.create_llm", lambda cfg: BatchLLM(fail_on_call=2))
    messages: list[str] = []
    out = tasks.localize_if_possible(_result(), cfg, [], lambda p, m: messages.append(m), tmp_path / "l.json")
    assert "專案" in out.segments[0].text and "項目" in out.segments[1].text
    assert any("已完成 1/4 段並保存" in m and "從中斷處繼續" in m for m in messages)
