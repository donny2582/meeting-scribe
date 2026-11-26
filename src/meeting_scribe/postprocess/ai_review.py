"""AI 校正的比對：逐字稿草稿（基準）與 AI 校正結果的對齊、變更標記、逐列「還原講者」、輸出 txt。

- 雲端比對：AI 校正前的逐字稿就是基準，套用後記下校正後的講者。
- 已有現成校正檔：以「逐字稿草稿」為主（left join），依時間碼對齊「逐字稿草稿_AI校正」；
  時間碼有容錯（預設 ±2 秒，取最接近、尚未用過的一列）。校正檔裡找不到對應、或該列沒有講者時，
  保留草稿的內容並標成淡黃色，讓使用者知道要自己檢查。兩邊都沒有時間碼時改為依順序對齊。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from meeting_scribe.models import RawSegment
from meeting_scribe.transcript_io import AiReview, TranscriptDocument, import_transcript

DEFAULT_TOLERANCE_SEC = 2.0
NO_SPEAKER = "講者"  # 匯入時沒有講者的列，會以這個名稱表示
CORRECTED_FILE = "逐字稿草稿_AI校正.txt"
DRAFT_FILE = "逐字稿草稿.txt"


def _has_speaker(seg: RawSegment) -> bool:
    return bool(seg.speaker.strip()) and seg.speaker != NO_SPEAKER


def align(base: list[RawSegment], corrected: list[RawSegment], timed: bool = True,
          tolerance: float = DEFAULT_TOLERANCE_SEC) -> tuple[list[RawSegment], list[int]]:
    """以 base 為主對齊 corrected，回傳（對齊後的片段, 找不到對應或沒有講者的列）。

    對齊後的片段沿用 base 的時間；有對應時採用校正檔的講者與內容（校正檔沒有講者時保留 base 的講者）。
    """
    used: set[int] = set()
    joined: list[RawSegment] = []
    unmatched: list[int] = []
    for i, seg in enumerate(base):
        match: int | None = None
        if timed:
            best = tolerance + 1e-9
            for j, cand in enumerate(corrected):
                if j in used:
                    continue
                diff = abs(cand.start - seg.start)
                if diff <= best:
                    best, match = diff, j
                    if diff == 0:
                        break
        elif i < len(corrected):
            match = i
        if match is None:
            joined.append(seg)
            unmatched.append(i)
            continue
        used.add(match)
        cand = corrected[match]
        speaker = cand.speaker if _has_speaker(cand) else seg.speaker
        text = cand.text.strip() or seg.text
        joined.append(seg.model_copy(update={"speaker": speaker, "text": text, "speaker_uncertain": False}))
        if not _has_speaker(cand):
            unmatched.append(i)
    return joined, unmatched


@dataclass
class LoadedReview:
    """讀取校正檔的結果。"""

    base: list[RawSegment]  # 逐字稿草稿（比對基準）
    joined: list[RawSegment]  # 以草稿為主對齊後的結果
    unmatched: list[int]  # 找不到對應或沒有講者的列（淡黃色）
    timed: bool  # 草稿有沒有時間碼（表格依此顯示時間碼）


def load_files(draft: Path, corrected: Path, work: Path,
               tolerance: float = DEFAULT_TOLERANCE_SEC) -> LoadedReview:
    """讀取逐字稿草稿與校正檔（txt、srt、docx、json 都可以，容錯解析），以草稿為主對齊。"""
    base_doc = import_transcript(draft, work / "base")
    corr_doc = import_transcript(corrected, work / "corrected")
    timed = base_doc.has_timecodes and corr_doc.has_timecodes
    joined, unmatched = align(base_doc.segments, corr_doc.segments, timed, tolerance)
    return LoadedReview(base_doc.segments, joined, unmatched, base_doc.has_timecodes)

def start_review(doc: TranscriptDocument, baseline: list[RawSegment], source: str,
                 unmatched: list[int] | None = None) -> None:
    """開始（或更新）比對狀態：baseline 與目前的片段一一對應。

    已經有比對狀態、而且列數相同時保留原本的基準（再跑一次 AI 校正，仍以最初的草稿比對）。
    """
    if doc.ai_review is not None and len(doc.ai_review.baseline) == len(doc.segments):
        baseline = doc.ai_review.baseline
    if len(baseline) != len(doc.segments):
        raise ValueError("比對基準與逐字稿的列數不同")
    doc.ai_review = AiReview(source=source, baseline=list(baseline),
                             corrected_speakers=[s.speaker for s in doc.segments],
                             unmatched=sorted(set(unmatched or [])), restored=[])


def speaker_changed(doc: TranscriptDocument, i: int) -> bool:
    """這一列的講者和基準不同（淡橘色）。"""
    r = doc.ai_review
    if r is None or i >= len(r.baseline):
        return False
    return doc.display_name(doc.segments[i].speaker) != doc.display_name(r.baseline[i].speaker)


def text_changed(doc: TranscriptDocument, i: int) -> bool:
    """這一列的內容和基準不同（淡橘色）。"""
    r = doc.ai_review
    return r is not None and i < len(r.baseline) and doc.segments[i].text != r.baseline[i].text


def can_restore(doc: TranscriptDocument, i: int) -> bool:
    """這一列可以勾選「還原講者」：AI 改過講者，或已經勾選還原。"""
    r = doc.ai_review
    if r is None or i >= len(r.baseline):
        return False
    return i in r.restored or r.corrected_speakers[i] != r.baseline[i].speaker


def set_restored(doc: TranscriptDocument, i: int, on: bool) -> bool:
    """勾選：講者回到基準（逐字稿草稿）；取消勾選：回到 AI 校正後的講者。有變更時回傳 True。"""
    r = doc.ai_review
    if r is None or i >= len(r.baseline):
        return False
    target = r.baseline[i].speaker if on else r.corrected_speakers[i]
    restored = set(r.restored)
    restored.add(i) if on else restored.discard(i)
    r.restored = sorted(restored)
    if doc.segments[i].speaker == target:
        return False
    doc.segments[i] = doc.segments[i].model_copy(update={"speaker": target})
    doc.edited = True
    return True


def _hms(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def write_txt(segments: list[RawSegment], path: Path, title: str, note: str,
              name_of: Any = None, timed: bool = True) -> Path:
    """寫成與「逐字稿草稿.txt」相同格式的文字檔（[時:分:秒] 講者：內容），可再匯入或比對。"""
    name_of = name_of or (lambda s: s)
    lines = [f"會議名稱：{title or '（未填）'}", note, f"產生時間：{datetime.now():%Y-%m-%d %H:%M}", ""]
    for s in segments:
        stamp = f"[{_hms(s.start)}] " if timed else ""
        lines.append(f"{stamp}{name_of(s.speaker)}：{s.text}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return path


def apply_roles_per_row(doc: TranscriptDocument, roles: list[Any]) -> int:
    """講者角色逐列套用（把該講者的每一列改成建議名稱），讓每一列都能單獨「還原講者」。回傳命名的講者數。"""
    named = 0
    for role in roles:
        name = role.name.strip()
        if not name or role.speaker not in doc.speakers():
            continue
        for i, seg in enumerate(doc.segments):
            if seg.speaker == role.speaker:
                doc.segments[i] = seg.model_copy(update={"speaker": name})
        doc.speaker_names.pop(role.speaker, None)
        named += 1
    if named:
        doc.edited = True
    return named
