"""Soniox 引擎（假的 HTTP 回應，不呼叫真實 API）與臺灣繁體轉換。"""

from pathlib import Path
from typing import Any

import httpx
import pytest

from meeting_scribe.config import load_config
from meeting_scribe.models import RawSegment
from meeting_scribe.postprocess.zh_tw import to_zh_tw
from meeting_scribe.stt import create_engine
from meeting_scribe.stt.base import SttError
from meeting_scribe.stt.soniox_stt import convert_chinese_segments, tokens_to_segments


def tok(text: str, start: int, end: int, speaker: str = "1", lang: str = "zh", **kw: Any) -> dict:
    return {"text": text, "start_ms": start, "end_ms": end, "confidence": 0.9,
            "speaker": speaker, "language": lang, **kw}


def test_to_zh_tw() -> None:
    assert to_zh_tw("这个软件的视频信息") == "這個軟體的影片資訊"
    assert to_zh_tw("公路局資料") == "公路局資料"  # 已是繁體不變
    assert to_zh_tw("") == ""


def test_tokens_grouped_by_speaker_language_and_gap() -> None:
    tokens = [
        tok("我们", 0, 300), tok("开始", 300, 600), tok("。", 600, 650),
        tok("Our", 700, 900, "2", "en"), tok(" team", 900, 1200, "2", "en"),
        tok("好", 5000, 5300, "1"),  # 停頓超過 1 秒
        tok("[music]", 5300, 6000, "1", is_audio_event=True),
    ]
    segs = tokens_to_segments(tokens)
    assert [(s.speaker, s.lang, s.text) for s in segs] == [
        ("1", "zh", "我们开始。"), ("2", "en", "Our team"), ("1", "zh", "好"),
    ]
    assert segs[0].start == 0 and segs[0].end == pytest.approx(0.65)
    assert segs[0].confidence == pytest.approx(0.9)


def test_convert_only_chinese_segments() -> None:
    segs = [
        RawSegment(start=0, end=1, speaker="1", text="这个软件", lang="zh"),
        RawSegment(start=1, end=2, speaker="2", text="日本の気象庁です", lang="ja"),
        RawSegment(start=2, end=3, speaker="2", text="気象庁", lang="ja"),  # 只有漢字但引擎說日文
        RawSegment(start=3, end=4, speaker="3", text="한국 경찰청", lang="ko"),
        RawSegment(start=4, end=5, speaker="1", text="用LiDAR测量", lang=None),
    ]
    out = [s.text for s in convert_chinese_segments(segs)]
    assert out == ["這個軟體", "日本の気象庁です", "気象庁", "한국 경찰청", "用LiDAR測量"]


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    monkeypatch.setenv("MEETINGSCRIBE_SONIOX_API_KEY", "sx-test")
    monkeypatch.setattr("meeting_scribe.stt.soniox_stt.probe_duration", lambda p, f: 600.0)
    monkeypatch.setattr("meeting_scribe.stt.soniox_stt.time.sleep", lambda s: None)
    audio = tmp_path / "a.flac"
    audio.write_bytes(b"x")
    cfg = load_config()
    cfg["stt"]["retries"] = 0
    calls: list[tuple[str, str, Any]] = []
    deleted: list[str] = []
    monkeypatch.setattr(httpx, "delete", lambda url, **k: deleted.append(url) or httpx.Response(204))
    return {"audio": audio, "cfg": cfg, "calls": calls, "deleted": deleted, "mp": monkeypatch}


def _router(calls: list, statuses: list[str], transcript_status: int = 200):  # type: ignore[no-untyped-def]
    def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        calls.append((method, url, kwargs.get("json")))
        req = httpx.Request(method, url)
        if url.endswith("/files"):
            return httpx.Response(201, json={"id": "file-1"}, request=req)
        if url.endswith("/transcriptions"):
            return httpx.Response(201, json={"id": "tr-1", "status": "queued"}, request=req)
        if url.endswith("/transcript"):
            return httpx.Response(transcript_status, json={"id": "tr-1", "text": "", "tokens": [
                tok("这个", 0, 400), tok("软件", 400, 800), tok("Hello", 900, 1200, "2", "en")]}, request=req)
        status = statuses.pop(0) if statuses else "completed"
        body = {"id": "tr-1", "status": status}
        if status == "error":
            body.update(error_type="bad_audio", error_message="無法解碼")
        return httpx.Response(200, json=body, request=req)

    return request


def test_soniox_transcribe(setup: dict[str, Any]) -> None:
    setup["mp"].setattr(httpx, "request", _router(setup["calls"], ["queued", "processing", "completed"]))
    engine = create_engine("soniox", setup["cfg"], Path("ffmpeg"))
    result = engine.transcribe(setup["audio"], ["zh-TW", "en-US", "ja-JP"], ["公路局", "LiDAR"],
                               work_dir=setup["audio"].parent)
    assert [s.text for s in result.segments] == ["這個軟體", "Hello"]
    assert [s.speaker for s in result.segments] == ["講者1", "講者2"]
    assert result.script_conversion == "s2twp" and result.needs_zh_tw_localization
    create = next(c for c in setup["calls"] if c[1].endswith("/transcriptions"))[2]
    assert create["model"] == "stt-async-v6"
    assert create["language_hints"] == ["zh", "en", "ja"]
    assert create["enable_speaker_diarization"] and create["enable_language_identification"]
    assert create["context"]["terms"] == ["公路局", "LiDAR"]
    assert any(g["key"] == "domain" for g in create["context"]["general"])
    assert setup["deleted"][0].endswith("/transcriptions/tr-1")
    assert setup["deleted"][1].endswith("/files/file-1")


def test_soniox_error_still_cleans_up(setup: dict[str, Any]) -> None:
    setup["mp"].setattr(httpx, "request", _router(setup["calls"], ["error"]))
    engine = create_engine("soniox", setup["cfg"], Path("ffmpeg"))
    with pytest.raises(SttError, match="無法解碼"):
        engine.transcribe(setup["audio"], ["zh-TW"], [], work_dir=setup["audio"].parent)
    assert len(setup["deleted"]) == 2


def test_soniox_cancel_during_polling(setup: dict[str, Any]) -> None:
    from meeting_scribe.tasks import Cancelled

    setup["mp"].setattr(httpx, "request", _router(setup["calls"], ["queued"] * 10))
    engine = create_engine("soniox", setup["cfg"], Path("ffmpeg"))

    def progress(pct: float, msg: str) -> None:
        if "排隊" in msg:
            raise Cancelled("已取消")

    with pytest.raises(Cancelled):
        engine.transcribe(setup["audio"], ["zh-TW"], [], progress=progress, work_dir=setup["audio"].parent)
    assert setup["deleted"] == []  # 取消時保留伺服器上的工作，下次可以直接取回
    assert (setup["audio"].parent / "soniox_job_a.json").exists()


def test_soniox_resumes_submitted_job_without_reupload(setup: dict[str, Any]) -> None:
    from meeting_scribe import checkpoint

    job = setup["audio"].parent / "soniox_job_a.json"
    checkpoint.write_json(job, {"file_id": "file-1", "transcription_id": "tr-1"})
    setup["mp"].setattr(httpx, "request", _router(setup["calls"], ["completed"]))
    engine = create_engine("soniox", setup["cfg"], Path("ffmpeg"))
    result = engine.transcribe(setup["audio"], ["zh-TW"], [], work_dir=setup["audio"].parent)
    urls = [c[1] for c in setup["calls"]]
    assert not any(u.endswith("/files") for u in urls)  # 沒有重新上傳
    assert not any(u.endswith("/transcriptions") for u in urls)  # 沒有重新建立（不會重複計費）
    assert result.segments[0].text == "這個軟體"
    assert not job.exists() and len(setup["deleted"]) == 2  # 取回後才清除


def test_soniox_upload_balance_error_cleans_up(setup: dict[str, Any]) -> None:
    def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        setup["calls"].append((method, url, None))
        req = httpx.Request(method, url)
        if url.endswith("/files"):
            return httpx.Response(201, json={"id": "file-1"}, request=req)
        return httpx.Response(402, json={"error_type": "organization_balance_exhausted"}, request=req)

    setup["mp"].setattr(httpx, "request", request)
    engine = create_engine("soniox", setup["cfg"], Path("ffmpeg"))
    with pytest.raises(SttError, match="餘額不足"):
        engine.transcribe(setup["audio"], ["zh-TW"], [], work_dir=setup["audio"].parent)
    assert any(u.endswith("/files/file-1") for u in setup["deleted"])  # 已上傳的檔案清掉
    assert not (setup["audio"].parent / "soniox_job_a.json").exists()


def test_soniox_balance_exhausted(setup: dict[str, Any]) -> None:
    def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(402, json={"error_type": "organization_balance_exhausted"},
                              request=httpx.Request(method, url))

    setup["mp"].setattr(httpx, "request", request)
    engine = create_engine("soniox", setup["cfg"], Path("ffmpeg"))
    with pytest.raises(SttError, match="餘額不足"):
        engine.transcribe(setup["audio"], ["zh-TW"], [], work_dir=setup["audio"].parent)


def test_soniox_missing_key(setup: dict[str, Any]) -> None:
    setup["mp"].delenv("MEETINGSCRIBE_SONIOX_API_KEY")
    setup["mp"].setattr("meeting_scribe.stt.soniox_stt.get_secret", lambda n: None)
    engine = create_engine("soniox", setup["cfg"], Path("ffmpeg"))
    with pytest.raises(SttError, match="Soniox API 金鑰"):
        engine.transcribe(setup["audio"], ["zh-TW"], [])
