"""逐字稿文件：從轉錄結果建立、從既有逐字稿檔匯入、儲存與讀取（「產出文件」分頁使用）。

可匯入的格式：
- 本程式的 ``stt_raw.json``（轉錄結果）或 ``document.json``（已存的文件）
- ``.txt``：每行「[時:分:秒] 講者：內容」（本程式的逐字稿草稿也是這個格式），或沒有時間碼的「講者：內容」、純文字
- ``.srt``：字幕檔
- ``.docx``：Word 逐字稿（逐段落以 .txt 規則解析）
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from meeting_scribe.models import RawSegment, SttResult

DOCUMENT_FILE = "document.json"
IMPORT_EXTENSIONS = (".txt", ".srt", ".docx", ".json")
MEETING_TYPES = {"topic": "工作進度會議（主題式）", "qa": "專家座談、問答型（問答式）", "agenda": "研商會、審查會（議程逐項式）"}

_TS = r"(\d{1,2}):(\d{2}):(\d{2})(?:[.,](\d{1,3}))?"
_LINE_RE = re.compile(rf"^\s*\[{_TS}\]\s*(.*)$")
_SPEAKER_RE = re.compile(r"^\s*([^：:\[\]]{1,20}?)\s*[：:]\s*(.*)$")
_SRT_TIME_RE = re.compile(rf"^\s*{_TS}\s*-->\s*{_TS}")
_UNCERTAIN = "（疑似）"


class ImportError_(Exception):
    """逐字稿檔無法匯入。"""


class MeetingInfo(BaseModel):
    """會議紀錄表頭與產出選項（SPEC 4.1）。"""

    title: str = ""
    date: str = Field(default_factory=lambda: date.today().isoformat())
    start_time: str = "14:00"
    end_time: str = ""
    location: str = ""
    chair: str = ""
    recorder: str = ""
    attendees: str = "（詳簽到簿）"
    agenda: list[str] = Field(default_factory=list)
    meeting_type: Literal["topic", "qa", "agenda"] = "topic"
    want_discussion: bool = True
    want_conclusion: bool = True
    want_action_table: bool = False
    want_mindmap: bool = False


class TranscriptDocument(BaseModel):
    """一份可編輯、可產出文件的逐字稿。"""

    source: str = Field(description="來源說明，例：轉錄結果、匯入的檔案路徑")
    folder: str = Field(default="", description="工作資料夾（document.json 與輸出檔的位置）")
    audio_name: str | None = None
    audio_duration: float | None = None
    engine: str | None = None
    has_timecodes: bool = True
    segments: list[RawSegment] = Field(default_factory=list)
    speaker_names: dict[str, str] = Field(default_factory=dict, description="講者代號 → 使用者命名")
    edited: bool = False
    info: MeetingInfo = Field(default_factory=MeetingInfo)

    # ---- 講者 ----
    def speakers(self) -> list[str]:
        """依第一次發言順序列出講者代號。"""
        return list(dict.fromkeys(s.speaker for s in self.segments))

    def display_name(self, speaker: str) -> str:
        return self.speaker_names.get(speaker) or speaker

    def rename_speaker(self, speaker: str, name: str) -> None:
        """講者命名（例：講者3 → 公路局）；空白表示取消命名。"""
        if name.strip() and name.strip() != speaker:
            self.speaker_names[speaker] = name.strip()
        else:
            self.speaker_names.pop(speaker, None)
        self.edited = True

    def merge_speaker(self, source: str, target: str) -> int:
        """把 source 的所有片段併入 target，回傳變更的片段數。"""
        if source == target:
            return 0
        changed = 0
        for i, seg in enumerate(self.segments):
            if seg.speaker == source:
                self.segments[i] = seg.model_copy(update={"speaker": target})
                changed += 1
        self.speaker_names.pop(source, None)
        self.edited = self.edited or changed > 0
        return changed

    # ---- 存讀 ----
    def save(self, folder: Path | None = None) -> Path:
        """存成 ``工作資料夾\\document.json``。"""
        target = Path(folder or self.folder)
        target.mkdir(parents=True, exist_ok=True)
        self.folder = str(target)
        path = target / DOCUMENT_FILE
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> TranscriptDocument:
        doc = cls.model_validate_json(path.read_text(encoding="utf-8"))
        doc.folder = str(path.parent)
        return doc


def from_stt_result(result: SttResult, folder: Path, audio: Path | None = None) -> TranscriptDocument:
    """由轉錄結果建立文件；資料夾裡已有修改過的 document.json 時沿用。"""
    existing = folder / DOCUMENT_FILE
    if existing.exists():
        return TranscriptDocument.load(existing)
    info = MeetingInfo()
    if audio and audio.exists():
        info.date = datetime.fromtimestamp(audio.stat().st_mtime).date().isoformat()  # 錄音檔日期≈會議日期
    source = "轉錄結果"
    if result.clip_end is not None:
        def hms(sec: float) -> str:
            s = int(sec)
            return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"

        source += f"（範圍 {hms(result.clip_start)}–{hms(result.clip_end)}）"
    return TranscriptDocument(
        source=source, folder=str(folder), audio_name=audio.name if audio else None,
        audio_duration=result.audio_duration, engine=result.engine_version,
        segments=list(result.segments), info=info,
    )


def _seconds(h: str, m: str, s: str, frac: str | None) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + (int(frac.ljust(3, "0")) / 1000 if frac else 0.0)


def _split_speaker(text: str) -> tuple[str, str, bool]:
    """「講者3（疑似）：內容」→（講者3, 內容, True）。沒有講者時回傳（"", 原文, False）。"""
    m = _SPEAKER_RE.match(text)
    if not m:
        return "", text.strip(), False
    speaker, body = m.group(1).strip(), m.group(2).strip()
    uncertain = speaker.endswith(_UNCERTAIN)
    return speaker.removesuffix(_UNCERTAIN).strip(), body, uncertain


def parse_lines(lines: list[str]) -> tuple[list[RawSegment], bool]:
    """解析逐行逐字稿，回傳（片段, 是否有時間碼）。"""
    stamped = [ln for ln in lines if _LINE_RE.match(ln)]
    segments: list[RawSegment] = []
    if stamped:
        for line in lines:
            m = _LINE_RE.match(line)
            if m:
                start = _seconds(*m.groups()[:4])
                speaker, body, uncertain = _split_speaker(m.group(5))
                segments.append(RawSegment(start=start, end=start, speaker=speaker or "講者", text=body,
                                           speaker_uncertain=uncertain))
            elif segments and line.strip():  # 時間碼下一行的內容，或換行接續
                prev = segments[-1]
                joined = (prev.text + "\n" + line.strip()).strip()
                segments[-1] = prev.model_copy(update={"text": joined})
        for i in range(len(segments) - 1):  # 結束時間＝下一段開始時間
            segments[i] = segments[i].model_copy(update={"end": segments[i + 1].start})
        return [s for s in segments if s.text], True
    for line in lines:
        if not line.strip():
            continue
        speaker, body, uncertain = _split_speaker(line)
        segments.append(RawSegment(start=0.0, end=0.0, speaker=speaker or "講者", text=body,
                                   speaker_uncertain=uncertain))
    return segments, False


def parse_srt(text: str) -> list[RawSegment]:
    """解析 .srt 字幕檔。"""
    segments: list[RawSegment] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        for idx, line in enumerate(lines):
            m = _SRT_TIME_RE.match(line)
            if m:
                g = m.groups()
                body = " ".join(lines[idx + 1:]).strip()
                speaker, body, uncertain = _split_speaker(body)
                if body:
                    segments.append(RawSegment(start=_seconds(*g[:4]), end=_seconds(*g[4:8]),
                                               speaker=speaker or "講者", text=body, speaker_uncertain=uncertain))
                break
    return segments


def import_transcript(path: Path, folder: Path) -> TranscriptDocument:
    """匯入既有逐字稿檔，建立新文件（資料夾為 folder）。"""
    suffix = path.suffix.lower()
    if suffix not in IMPORT_EXTENSIONS:
        raise ImportError_(f"不支援的檔案格式：{suffix}（支援 txt、srt、docx、json）")
    try:
        if suffix == ".json":
            raw = path.read_text(encoding="utf-8")
            if '"segments"' in raw and '"info"' in raw:
                doc = TranscriptDocument.model_validate_json(raw)
                doc.folder = str(folder)
                return doc
            result = SttResult.model_validate_json(raw)
            doc = from_stt_result(result, folder)
            doc.source = f"匯入：{path.name}"
            return doc
        if suffix == ".srt":
            segments, timed = parse_srt(path.read_text(encoding="utf-8-sig")), True
        elif suffix == ".docx":
            import docx

            segments, timed = parse_lines([p.text for p in docx.Document(str(path)).paragraphs])
        else:
            segments, timed = parse_lines(_read_text(path).splitlines())
    except ImportError_:
        raise
    except Exception as exc:
        raise ImportError_(f"無法讀取 {path.name}：{exc}") from exc
    if not segments:
        raise ImportError_(f"{path.name} 裡沒有可用的逐字稿內容")
    duration = max((s.end for s in segments), default=0.0) or None
    return TranscriptDocument(source=f"匯入：{path.name}", folder=str(folder), has_timecodes=timed,
                              audio_duration=duration, segments=segments)


def _read_text(path: Path) -> str:
    """讀取文字檔：先試 UTF-8，再試 Big5（舊版 Windows 記事本存的繁體中文）。"""
    data = path.read_bytes()
    for enc in ("utf-8-sig", "cp950"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ImportError_("無法辨識文字編碼，請另存為 UTF-8")
