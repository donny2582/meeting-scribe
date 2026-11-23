"""STT 引擎（以假的 HTTP 回應測試，不呼叫真實 API）。"""

from pathlib import Path
from typing import Any

import httpx
import pytest

from meeting_scribe.config import load_config
from meeting_scribe.stt import base as stt_base
from meeting_scribe.stt import create_engine
from meeting_scribe.stt.base import SttError, request_with_retry


@pytest.fixture
def audio(tmp_path: Path) -> Path:
    p = tmp_path / "audio.flac"
    p.write_bytes(b"fake")
    return p


@pytest.fixture
def cfg() -> dict[str, Any]:
    c = load_config()
    c["stt"]["azure"]["region"] = "eastasia"
    c["stt"]["retries"] = 0
    return c


def _fake_post(payload: dict, status: int = 200, calls: list | None = None):  # type: ignore[no-untyped-def]
    def post(url: str, **kwargs: Any) -> httpx.Response:
        if calls is not None:
            calls.append((url, kwargs))
        return httpx.Response(status, json=payload, request=httpx.Request("POST", url))

    return post


def test_azure_transcribe(monkeypatch: pytest.MonkeyPatch, audio: Path, cfg: dict) -> None:
    monkeypatch.setenv("MEETINGSCRIBE_AZURE_SPEECH_KEY", "test-key-1234")
    monkeypatch.setattr("meeting_scribe.stt.azure_speech.probe_duration", lambda p, f: 10.0)
    calls: list = []
    payload = {"phrases": [
        {"speaker": 2, "offsetMilliseconds": 0, "durationMilliseconds": 1000, "text": "大家好", "locale": "zh-TW"},
        {"speaker": 1, "offsetMilliseconds": 1500, "durationMilliseconds": 1000, "text": "Hello", "locale": "en-US"},
    ]}
    monkeypatch.setattr(httpx, "post", _fake_post(payload, calls=calls))
    engine = create_engine("azure", cfg, Path("ffmpeg"))
    result = engine.transcribe(audio, ["zh-TW", "en-US"], ["公路局"], None, work_dir=audio.parent)
    assert [s.speaker for s in result.segments] == ["講者1", "講者2"]
    url, kwargs = calls[0]
    assert url.startswith("https://eastasia.api.cognitive.microsoft.com/speechtotext/")
    assert kwargs["headers"]["Ocp-Apim-Subscription-Key"] == "test-key-1234"
    assert "phraseList" in kwargs["files"]["definition"][1]


def test_azure_bad_key(monkeypatch: pytest.MonkeyPatch, audio: Path, cfg: dict) -> None:
    monkeypatch.setenv("MEETINGSCRIBE_AZURE_SPEECH_KEY", "bad")
    monkeypatch.setattr("meeting_scribe.stt.azure_speech.probe_duration", lambda p, f: 10.0)
    monkeypatch.setattr(httpx, "post", _fake_post({"error": "x"}, status=401))
    engine = create_engine("azure", cfg, Path("ffmpeg"))
    with pytest.raises(SttError, match="金鑰無效"):
        engine.transcribe(audio, ["zh-TW"], [], None, work_dir=audio.parent)


def test_openai_transcribe_chunked(monkeypatch: pytest.MonkeyPatch, audio: Path, cfg: dict) -> None:
    monkeypatch.setenv("MEETINGSCRIBE_OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("meeting_scribe.stt.openai_stt.probe_duration", lambda p, f: 2500.0)
    exported: list[tuple[float, float]] = []

    def fake_export(src: Path, dst: Path, ffmpeg: Path, start: float, dur: float, bitrate: Any = None) -> Path:
        exported.append((start, dur))
        dst.write_bytes(b"x")
        return dst

    monkeypatch.setattr("meeting_scribe.audio.preprocess.export_chunk", fake_export)
    payload = {"segments": [{"speaker": "A", "start": 0.0, "end": 5.0, "text": "大家好"}]}
    calls: list = []
    monkeypatch.setattr(httpx, "post", _fake_post(payload, calls=calls))
    engine = create_engine("openai", cfg, Path("ffmpeg"))
    result = engine.transcribe(audio, ["zh-TW", "en-US"], [], None, work_dir=audio.parent)
    assert len(exported) == 3  # 2500 秒、每段 1200 秒、重疊 30 秒
    assert len(calls) == 3
    assert calls[0][1]["data"]["response_format"] == "diarized_json"
    assert "language" not in calls[0][1]["data"]  # 多語言時不指定
    assert len(result.raw_responses) == 3


def test_missing_key(monkeypatch: pytest.MonkeyPatch, audio: Path, cfg: dict) -> None:
    monkeypatch.delenv("MEETINGSCRIBE_OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("meeting_scribe.stt.openai_stt.get_secret", lambda name: None)
    engine = create_engine("openai", cfg, Path("ffmpeg"))
    with pytest.raises(SttError, match="尚未設定"):
        engine.transcribe(audio, ["zh-TW"], [], None)


def test_retry_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stt_base.time, "sleep", lambda s: None)
    req = httpx.Request("POST", "https://x")
    responses = [httpx.ConnectError("boom"), httpx.Response(503, request=req), httpx.Response(200, request=req)]

    def send() -> httpx.Response:
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    assert request_with_retry(send, retries=3).status_code == 200


def test_retry_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stt_base.time, "sleep", lambda s: None)

    def send() -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(SttError, match="已重試"):
        request_with_retry(send, retries=2)


def test_unknown_engine(cfg: dict) -> None:
    with pytest.raises(SttError):
        create_engine("nope", cfg, Path("ffmpeg"))
