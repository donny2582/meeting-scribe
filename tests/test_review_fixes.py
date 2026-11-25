"""程式碼檢查發現的問題（重現測試）：引擎評估共用暫存、AI 整理快取條件、Soniox 續跑、未記錄的用量、刪贅字續跑。"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from meeting_scribe import tasks, usage
from meeting_scribe.config import load_config
from meeting_scribe.llm.base import LLMClient, LLMError
from meeting_scribe.llm.openai_llm import OpenAILLM
from meeting_scribe.models import RawSegment, SttResult
from meeting_scribe.postprocess.lang_detect import LanguageRegistry
from meeting_scribe.stt import create_engine
from meeting_scribe.stt.base import SttError


# ---- 1. 引擎評估：每個錄音、每個引擎要用各自的暫存資料夾 ----
def test_benchmark_each_audio_and_engine_has_own_work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str, Path]] = []

    def fake(engine_name: str, audio: Path, work_dir: Path, **kwargs: Any) -> SttResult:
        seen.append((engine_name, audio.name, work_dir))
        return SttResult(engine=engine_name, engine_version="v", audio_duration=10, elapsed_sec=1, locales=["zh-TW"],
                         segments=[RawSegment(start=0, end=2, speaker="講者1", text="開會")])

    monkeypatch.setattr(tasks, "transcribe_audio", fake)
    main, lang = tmp_path / "main.m4a", tmp_path / "lang.m4a"
    out = tmp_path / "eval"
    stale = out / "_work" / "main_azure" / "audio.flac"  # 上一次評估留下的另一段錄音
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    tasks.run_benchmark(main, None, ["azure", "openai"], ["zh"], [], load_config(), LanguageRegistry.load(), out,
                        lang_audio=lang)
    dirs = [d for _, _, d in seen]
    assert len(seen) == 4 and len(set(dirs)) == 4  # 不同錄音、不同引擎不會共用切段結果與音訊
    assert not stale.exists()  # 重新評估時清掉舊的暫存，不會沿用上一段錄音


# ---- 2. AI 整理：只有送給 AI 的內容改變才重新整理 ----
class _FakeLLM(LLMClient):
    name = "fake"

    def __init__(self) -> None:
        super().__init__()
        self.models = {"summary": "fake-sonnet"}
        self.calls = 0

    def complete_json(self, system: str, user: str, schema: dict, task: str, max_tokens: int = 16000) -> dict:
        self.calls += 1
        return {"meeting_type": "topic", "discussion_items": [{"title": "毒駕", "children": [], "evidence": ["S0001"]}],
                "conclusions": [], "action_items": [], "mindmap": {"text": "會議", "children": []}, "warnings": []}


def test_summary_cache_ignores_fields_ai_does_not_see(tmp_path: Path) -> None:
    from meeting_scribe.summarize.summarizer import make_line, summarize

    lines = [make_line("S0001", 0, "講者1", "毒駕修法今年底完成。")]
    info = {"title": "研商會", "meeting_type": "topic", "chair": "", "agenda": [], "location": "1609 會議室",
            "want_mindmap": False, "end_time": "16:00"}
    llm = _FakeLLM()
    summarize(lines, info, ["講者1"], llm, load_config(), "", None, tmp_path)
    changed = dict(info, location="大禮堂", want_mindmap=True, end_time="16:30", recorder="王小明")
    summarize(lines, changed, ["講者1"], llm, load_config(), "", None, tmp_path)
    assert llm.calls == 1  # 改地點、勾心智圖、改散會時間 → 沿用，不重複計費
    summarize(lines, dict(info, title="另一場會議"), ["講者1"], llm, load_config(), "", None, tmp_path)
    assert llm.calls == 2  # 會議名稱會送給 AI → 重新整理


def test_summary_cache_from_previous_version_still_reused(tmp_path: Path) -> None:
    """舊版程式存的結果（快取條件包含全部會議資訊）仍可沿用，不會因為改版重新計費。"""
    from meeting_scribe.summarize import summarizer

    lines = [summarizer.make_line("S0001", 0, "講者1", "毒駕修法今年底完成。")]
    info = {"title": "研商會", "meeting_type": "topic", "location": "1609"}
    llm = _FakeLLM()
    old_key = summarizer._key([summarizer.PROMPT_VERSION, "fake-sonnet", "topic", info, ["講者1"], ""],
                              [line.text for line in lines])
    summary = summarizer.Summary.model_validate(_FakeLLM().complete_json("", "", {}, "summary"))
    summarizer._save(tmp_path / "summary_topic.json", old_key, summary)
    summarizer.summarize(lines, info, ["講者1"], llm, load_config(), "", None, tmp_path)
    assert llm.calls == 0


# ---- 3. Soniox 續跑：連線失敗時保留已送出的工作，不重新送出 ----
@pytest.fixture
def soniox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    from meeting_scribe import checkpoint

    monkeypatch.setattr("meeting_scribe.stt.soniox_stt.get_secret", lambda n: "sx-test")
    monkeypatch.setattr("meeting_scribe.stt.soniox_stt.probe_duration", lambda p, f: 600.0)
    monkeypatch.setattr("meeting_scribe.stt.soniox_stt.time.sleep", lambda s: None)
    monkeypatch.setattr("meeting_scribe.stt.base.time.sleep", lambda s: None)
    audio = tmp_path / "a.flac"
    audio.write_bytes(b"x")
    job = tmp_path / "soniox_job_a.json"
    checkpoint.write_json(job, {"file_id": "file-old", "transcription_id": "tr-old"})
    cfg = load_config()
    cfg["stt"]["retries"] = 0
    calls: list[str] = []
    deleted: list[str] = []
    monkeypatch.setattr(httpx, "delete", lambda url, **k: deleted.append(url) or httpx.Response(204))
    return {"audio": audio, "job": job, "cfg": cfg, "calls": calls, "deleted": deleted, "mp": monkeypatch}


def test_soniox_resume_network_error_keeps_job(soniox: dict[str, Any]) -> None:
    def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        soniox["calls"].append(url)
        raise httpx.ConnectError("網路中斷")

    soniox["mp"].setattr(httpx, "request", request)
    engine = create_engine("soniox", soniox["cfg"], Path("ffmpeg"))
    with pytest.raises(SttError):
        engine.transcribe(soniox["audio"], ["zh-TW"], [], work_dir=soniox["audio"].parent)
    assert soniox["job"].exists()  # 工作紀錄還在，網路恢復後可以直接取回
    assert not any(u.endswith("/files") for u in soniox["calls"])  # 沒有重新上傳（不會重複計費）
    assert soniox["deleted"] == []


def test_soniox_resume_job_gone_resubmits(soniox: dict[str, Any]) -> None:
    def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        soniox["calls"].append(url)
        req = httpx.Request(method, url)
        if url.endswith("/transcriptions/tr-old"):
            return httpx.Response(404, json={"error": "not found"}, request=req)
        if url.endswith("/files"):
            return httpx.Response(201, json={"id": "file-1"}, request=req)
        if url.endswith("/transcriptions"):
            return httpx.Response(201, json={"id": "tr-1"}, request=req)
        if url.endswith("/transcript"):
            return httpx.Response(200, json={"id": "tr-1", "tokens": []}, request=req)
        return httpx.Response(200, json={"id": "tr-1", "status": "completed"}, request=req)

    soniox["mp"].setattr(httpx, "request", request)
    engine = create_engine("soniox", soniox["cfg"], Path("ffmpeg"))
    engine.transcribe(soniox["audio"], ["zh-TW"], [], work_dir=soniox["audio"].parent)
    assert any(u.endswith("/files") for u in soniox["calls"])  # 伺服器上真的沒有了 → 重新送出


# ---- 4. AI 呼叫已計費但結果不能用（拒絕、超過長度）時也要記錄用量 ----
def test_openai_usage_recorded_even_when_output_unusable(monkeypatch: pytest.MonkeyPatch) -> None:
    def post(url: str, json: dict, headers: dict, timeout: Any) -> httpx.Response:
        body = {"model": "gpt-6.2-sol", "usage": {"prompt_tokens": 1000, "completion_tokens": 16000},
                "choices": [{"finish_reason": "length", "message": {"content": "{\"items\": ["}}]}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    cfg = load_config()
    cfg["llm"]["max_retries"] = 0
    with pytest.raises(LLMError):
        OpenAILLM(cfg, api_key="sk-test").complete_json("s", "u", {}, "summary")
    recs = usage.load_records()
    assert len(recs) == 1 and recs[0].output_tokens == 16000


def test_anthropic_usage_recorded_even_when_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from meeting_scribe.llm.anthropic_llm import AnthropicLLM

    llm = AnthropicLLM(load_config(), api_key="sk-ant-test")
    reply = SimpleNamespace(stop_reason="refusal", stop_details=None, content=[], model="claude-sonnet-5-5",
                            usage=SimpleNamespace(input_tokens=500, output_tokens=20, cache_creation_input_tokens=0,
                                                  cache_read_input_tokens=0))
    monkeypatch.setattr(llm.client.beta.messages, "create", lambda **k: reply)
    with pytest.raises(LLMError, match="拒絕"):
        llm.complete_json("s", "u", {}, "localize")
    recs = usage.load_records()
    assert len(recs) == 1 and recs[0].input_tokens == 500


# ---- 5. 刪贅字：AI 漏回傳某段時，已處理的批次下次仍可沿用 ----
def test_filler_batch_reused_even_if_ai_skipped_an_item(tmp_path: Path) -> None:
    from meeting_scribe.postprocess.filler import clean_texts

    class Skip:
        calls = 0

        def complete_json(self, *a: Any, **k: Any) -> dict:
            Skip.calls += 1
            return {"items": [{"id": "S1", "text": "我們開始"}]}  # 漏了 S2

    texts = [("S1", "呃我們開始"), ("S2", "嗯好")]
    ckpt = tmp_path / "filler.json"
    clean_texts(texts, lambda sid, t: t, Skip(), 3000, None, ckpt)
    clean_texts(texts, lambda sid, t: t, Skip(), 3000, None, ckpt)
    assert Skip.calls == 1  # 第二次沿用，不重複計費
    assert "S2" in json.loads(ckpt.read_text(encoding="utf-8"))
