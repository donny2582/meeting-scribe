"""長音檔切段與跨段講者串接（SPEC 5.2）。

各段之間重疊 overlap 秒。串接時，比對「前一段」與「這一段」在重疊區域內的講者發言時間，
重疊時間最長的講者配成同一人（一對一、貪婪配對）；對應不到的講者給新的代號，
並把他在該段的所有片段標為講者不確定（疑似）。重疊區域以中點為界，
中點之前採用前一段的片段，中點之後採用這一段的片段，避免重複。
"""

from __future__ import annotations

from dataclasses import dataclass

from meeting_scribe.models import RawSegment


@dataclass(frozen=True)
class ChunkSpan:
    """切段範圍（秒，絕對時間）。"""

    index: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def plan_chunks(duration: float, chunk_sec: float, overlap_sec: float) -> list[ChunkSpan]:
    """規劃切段；不超過 chunk_sec 時只有一段。"""
    if duration <= chunk_sec:
        return [ChunkSpan(0, 0.0, duration)]
    if chunk_sec <= overlap_sec:
        raise ValueError("chunk_sec 必須大於 overlap_sec")
    spans: list[ChunkSpan] = []
    start = 0.0
    step = chunk_sec - overlap_sec
    while True:
        end = min(start + chunk_sec, duration)
        spans.append(ChunkSpan(len(spans), start, end))
        if end >= duration:
            break
        start += step
    return spans


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def merge_chunk_results(
    chunks: list[tuple[ChunkSpan, list[RawSegment]]], min_match_sec: float = 1.0
) -> list[RawSegment]:
    """把各段結果（片段時間為段內相對時間）串接成完整結果。

    回傳的講者代號為內部代號（G1、G2…），之後再由 renumber_speakers 統一編號。
    """
    result: list[RawSegment] = []
    prev_span: ChunkSpan | None = None
    prev_segs: list[RawSegment] = []
    next_id = 1

    for span, local in chunks:
        shifted = [s.model_copy(update={"start": s.start + span.start, "end": s.end + span.start}) for s in local]
        local_speakers = list(dict.fromkeys(s.speaker for s in sorted(shifted, key=lambda x: x.start)))
        mapping: dict[str, str] = {}
        uncertain: set[str] = set()

        if prev_span is not None:
            w0, w1 = span.start, prev_span.end
            scores: dict[tuple[str, str], float] = {}
            for p in prev_segs:
                pa, pb = max(p.start, w0), min(p.end, w1)
                if pb <= pa:
                    continue
                for c in shifted:
                    ov = _overlap(pa, pb, max(c.start, w0), min(c.end, w1))
                    if ov > 0:
                        scores[(p.speaker, c.speaker)] = scores.get((p.speaker, c.speaker), 0.0) + ov
            used_global: set[str] = set()
            for (g, loc), score in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
                if score < min_match_sec or g in used_global or loc in mapping:
                    continue
                mapping[loc] = g
                used_global.add(g)

        for loc in local_speakers:
            if loc not in mapping:
                mapping[loc] = f"G{next_id}"
                next_id += 1
                if prev_span is not None:
                    uncertain.add(loc)

        labeled = [
            s.model_copy(
                update={
                    "speaker": mapping[s.speaker],
                    "speaker_uncertain": s.speaker_uncertain or s.speaker in uncertain,
                }
            )
            for s in shifted
        ]
        if prev_span is None:
            result.extend(labeled)
        else:
            mid = (span.start + prev_span.end) / 2
            result = [s for s in result if s.start < mid]
            result.extend(s for s in labeled if s.start >= mid)
        prev_span, prev_segs = span, labeled

    return sorted(result, key=lambda s: (s.start, s.end))
