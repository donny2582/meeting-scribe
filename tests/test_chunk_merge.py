"""長音檔切段與跨段講者串接（SPEC 5.2）。"""

import pytest

from meeting_scribe.models import RawSegment
from meeting_scribe.stt.base import renumber_speakers
from meeting_scribe.stt.chunk_merge import ChunkSpan, merge_chunk_results, plan_chunks


def seg(start: float, end: float, spk: str, text: str = "x") -> RawSegment:
    return RawSegment(start=start, end=end, speaker=spk, text=text)


def test_plan_single_chunk() -> None:
    assert plan_chunks(600, 1200, 30) == [ChunkSpan(0, 0.0, 600)]


def test_plan_overlapping_chunks() -> None:
    spans = plan_chunks(3000, 1200, 30)
    assert [(s.start, s.end) for s in spans] == [(0, 1200), (1170, 2370), (2340, 3000)]


def test_plan_invalid() -> None:
    with pytest.raises(ValueError):
        plan_chunks(100, 30, 30)


def test_merge_maps_speakers_by_overlap() -> None:
    c0 = ChunkSpan(0, 0.0, 100.0)
    c1 = ChunkSpan(1, 70.0, 170.0)
    # 第一段：A 在 0–40、80–90；B 在 40–80、90–100
    chunk0 = [seg(0, 40, "A"), seg(40, 80, "B"), seg(80, 90, "A"), seg(90, 100, "B")]
    # 第二段（相對時間）：同一批人但引擎給了不同代號（Y＝B、X＝A）；另外出現新講者 Z
    chunk1 = [seg(0, 10, "Y"), seg(10, 20, "X"), seg(20, 30, "Y"), seg(50, 60, "Z")]
    merged = merge_chunk_results([(c0, chunk0), (c1, chunk1)])

    by_time = {(s.start, s.end): s for s in merged}
    assert by_time[(80, 90)].speaker == by_time[(0, 40)].speaker  # 第一段 A
    # 中點 85 之後只取第二段：第二段的 A（90–100 絕對時間）對到第一段的 B
    assert by_time[(90.0, 100.0)].speaker == by_time[(40, 80)].speaker
    new = by_time[(120.0, 130.0)]
    assert new.speaker_uncertain  # 新講者對應不到 → 疑似
    assert not by_time[(0, 40)].speaker_uncertain
    # 重疊區不重複
    assert len([s for s in merged if 70 <= s.start < 100]) == 2


def test_renumber_by_first_appearance() -> None:
    segs = [seg(5, 6, "X"), seg(0, 1, "Y"), seg(2, 3, "X")]
    out = renumber_speakers(segs)
    assert [s.speaker for s in out] == ["講者1", "講者2", "講者2"]
