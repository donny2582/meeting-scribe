"""第 0 階段評估指標與引擎回應解析。"""

from pathlib import Path

import pytest

from meeting_scribe.benchmark import (
    EngineReport,
    RefSegment,
    cer,
    glossary_hits,
    language_metrics,
    load_reference,
    parse_time,
    render_report,
    speaker_error_ratio,
    write_reference_draft,
)
from meeting_scribe.config import load_config
from meeting_scribe.models import RawSegment
from meeting_scribe.postprocess.lang_detect import LanguageRegistry
from meeting_scribe.stt.azure_speech import parse_azure_response
from meeting_scribe.stt.openai_stt import parse_openai_response


@pytest.fixture(scope="module")
def reg() -> LanguageRegistry:
    return LanguageRegistry.load()


def test_parse_time() -> None:
    assert parse_time("00:01:02.50") == pytest.approx(62.5)
    assert parse_time("1:02") == pytest.approx(62)
    assert parse_time("12.3") == pytest.approx(12.3)


def test_cer_ignores_fillers_and_punct(reg: LanguageRegistry) -> None:
    assert cer("我們開始開會。", "呃，我們開始開會", reg) == 0.0
    assert cer("我們開始開會", "我們開使開會", reg) == pytest.approx(1 / 6)


def test_speaker_error_with_label_mapping() -> None:
    ref = [RefSegment(0, 10, "甲", "zh", "a"), RefSegment(10, 20, "乙", "zh", "b")]
    hyp_perfect = [RawSegment(start=0, end=10, speaker="講者2", text="a"),
                   RawSegment(start=10, end=20, speaker="講者1", text="b")]
    assert speaker_error_ratio(ref, hyp_perfect) == pytest.approx(0.0, abs=0.002)
    hyp_half = [RawSegment(start=0, end=20, speaker="講者1", text="ab")]
    assert speaker_error_ratio(ref, hyp_half) == pytest.approx(0.5, abs=0.002)


def test_glossary_hits() -> None:
    assert glossary_hits("公路局與公路局及LiDAR", "公路局與工路局及lidar", ["公路局", "LiDAR", "高公局"]) == (2, 3)


def test_language_metrics(reg: LanguageRegistry) -> None:
    ref = [
        RefSegment(0, 5, "甲", "zh", "我們開始開會"),
        RefSegment(5, 10, "乙", "ja", "日本では始まりました"),
        RefSegment(10, 15, "丙", "ko", "경찰청에서 운영합니다"),
    ]
    hyp = [
        RawSegment(start=0, end=5, speaker="1", text="我們開始開會"),
        RawSegment(start=5, end=10, speaker="2", text="日本では始まりました"),
        RawSegment(start=10, end=15, speaker="3", text="警察廳營運"),  # 韓文被辨識成中文
    ]
    (ok, total), (zh_wrong, zh_total) = language_metrics(ref, hyp, reg)
    assert (ok, total) == (1, 2)
    assert (zh_wrong, zh_total) == (0, 1)


def test_reference_roundtrip(tmp_path: Path, reg: LanguageRegistry) -> None:
    path = tmp_path / "ref.csv"
    segs = [RawSegment(start=1.5, end=3.25, speaker="講者1", text="Hello everyone, welcome.")]
    write_reference_draft(segs, path, reg)
    ref = load_reference(path)
    assert ref[0].start == pytest.approx(1.5)
    assert ref[0].lang == "en"


def test_parse_azure_response() -> None:
    data = {
        "durationMilliseconds": 5000,
        "phrases": [
            {"speaker": 1, "offsetMilliseconds": 400, "durationMilliseconds": 1600,
             "text": "好，我們開始。", "locale": "zh-TW", "confidence": 0.91},
            {"speaker": 2, "offsetMilliseconds": 2000, "durationMilliseconds": 1000, "text": "  "},
        ],
    }
    segs = parse_azure_response(data)
    assert len(segs) == 1
    assert segs[0].start == pytest.approx(0.4)
    assert segs[0].end == pytest.approx(2.0)
    assert segs[0].speaker == "1" and segs[0].lang == "zh-TW"


def test_parse_openai_response() -> None:
    data = {"segments": [{"type": "transcript.text.segment", "speaker": "A", "start": 0.0, "end": 2.5,
                          "text": "大家好"}]}
    segs = parse_openai_response(data)
    assert segs[0].speaker == "A" and segs[0].end == pytest.approx(2.5)


def test_render_report() -> None:
    cfg = load_config()
    r = EngineReport("azure", "v", 600, 60, 0.0, cer=0.1, speaker_error=0.3,
                     glossary=(8, 10), lang_acc=(9, 10), zh_misjudge=(0, 50))
    md = render_report([r], cfg["benchmark"], "a.m4a", None)
    assert "10.0% ✔" in md
    assert "30.0% ✘" in md
    assert "未設定單價" in md
    assert "12.0 分鐘" in md  # 10 分鐘錄音花 1 分鐘 → 2 小時約 12 分鐘
