"""逐字稿文件：從轉錄結果建立、從既有逐字稿檔匯入、儲存與讀取（「產出文件」分頁使用）。

可匯入的格式：
- 本程式的 ``stt_raw.json``（轉錄結果）或 ``document.json``（已存的文件）
- ``.txt``：有容錯，常見寫法都可以：
  「[00:04:07] 講者1：內容」（本程式的逐字稿草稿）、
  「00:04:07 演講者 1」換行再寫內容（會議軟體匯出的逐字稿）、
  「演講者 1  00:04:07」換行再寫內容、「4:07 主席：內容」；
  也可以沒有時間碼（「講者：內容」或純文字）。
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
    clean_fillers: bool = True  # 產出的逐字稿刪除贅字（只刪不改；畫面上的原文不變）
    want_transcript: bool = True  # 產出逐字稿 .docx（選用；產出項目至少要選一項）


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
    original_segments: list[RawSegment] | None = Field(
        default=None, description="最初（轉錄或匯入時）的逐字稿，「重新來過」用；舊版存的文件沒有這一項")

    def model_post_init(self, _context: object) -> None:
        """新建立的文件記下最初的內容（從 document.json 讀入時保留檔案裡的紀錄）。"""
        if self.original_segments is None and not self.edited and not self.speaker_names:
            self.original_segments = list(self.segments)

    # ---- 重新來過 ----
    def has_original(self) -> bool:
        return self.original_segments is not None

    def reset_to_original(self) -> bool:
        """逐字稿內容與講者還原成最初的樣子（會議資訊不變）。沒有最初的紀錄時回傳 False。"""
        if self.original_segments is None:
            return False
        self.segments = list(self.original_segments)
        self.speaker_names = {}
        self.edited = False
        return True

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


class EditHistory:
    """逐字稿編輯的復原／重做（只在這次開啟期間有效，不存檔）。

    每次修改前呼叫 ``record(文件, 動作說明)`` 記下修改前的逐字稿片段與講者命名；
    片段本身不會被原地修改（都用 model_copy 換成新物件），所以只需複製清單，很省記憶體。
    """

    LIMIT = 200

    def __init__(self) -> None:
        self._undo: list[tuple[str, list[RawSegment], dict[str, str]]] = []
        self._redo: list[tuple[str, list[RawSegment], dict[str, str]]] = []

    @staticmethod
    def _snapshot(doc: TranscriptDocument, label: str) -> tuple[str, list[RawSegment], dict[str, str]]:
        return label, list(doc.segments), dict(doc.speaker_names)

    @staticmethod
    def _apply(doc: TranscriptDocument, state: tuple[str, list[RawSegment], dict[str, str]]) -> None:
        _label, segments, names = state
        doc.segments = list(segments)
        doc.speaker_names = dict(names)
        doc.edited = True

    def record(self, doc: TranscriptDocument, label: str) -> None:
        """修改前呼叫：記下目前的狀態。新的修改會清掉「重做」。"""
        self._undo.append(self._snapshot(doc, label))
        del self._undo[:-self.LIMIT]
        self._redo.clear()

    def undo(self, doc: TranscriptDocument) -> str | None:
        """回到上一步，回傳被復原的動作說明；沒有可以復原時回傳 None。"""
        if not self._undo:
            return None
        state = self._undo.pop()
        self._redo.append(self._snapshot(doc, state[0]))
        self._apply(doc, state)
        return state[0]

    def redo(self, doc: TranscriptDocument) -> str | None:
        """重做剛才復原的動作，回傳動作說明；沒有時回傳 None。"""
        if not self._redo:
            return None
        state = self._redo.pop()
        self._undo.append(self._snapshot(doc, state[0]))
        self._apply(doc, state)
        return state[0]

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()

    @property
    def undo_label(self) -> str | None:
        return self._undo[-1][0] if self._undo else None

    @property
    def redo_label(self) -> str | None:
        return self._redo[-1][0] if self._redo else None


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


def _seconds(h: str | None, m: str, s: str, frac: str | None) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + (int(frac.ljust(3, "0")) / 1000 if frac else 0.0)


# ---- 容錯解析 ----
# 時間碼：時:分:秒 或 分:秒，可帶毫秒；可有 [ ]、( )、（ ）包住；冒號全形半形皆可
_FLEX_TS = r"(?:(\d{1,2})[:：])?(\d{1,2})[:：](\d{2})(?:[.,](\d{1,3}))?"
_OPEN, _CLOSE = r"[\[(（【]?", r"[\])）】]?"
# 行首是時間碼：「00:04:07 演講者 1」「[00:04:07] 講者1：內容」「0:04 - 主席：內容」
_HEAD_RE = re.compile(rf"^\s*{_OPEN}{_FLEX_TS}{_CLOSE}\s*(?:[-–—|｜]\s*)?(.*)$")
# 行尾是時間碼（講者在前）：「演講者 1  00:04:07」「主席 (4:07)」
_TAIL_RE = re.compile(rf"^\s*([^\s：:，。！？,!?][^：:，。！？,!?]{{0,19}}?)\s*[-–—|｜]?\s*{_OPEN}{_FLEX_TS}{_CLOSE}\s*$")
# 「演講者 1」「說話者2」「Speaker 3」「發言人 4」→ 講者N
_GENERIC_SPEAKER_RE = re.compile(r"^(?:演講者|說話者|說話人|發言者|發言人|講者|speaker|spk)\s*(\d+)$", re.IGNORECASE)
_SENTENCE_PUNCT = re.compile(r"[，。！？；,.!?;、]")


def normalize_speaker(name: str) -> str:
    """統一講者名稱：「演講者 1」「Speaker 1」→「講者1」；其他名稱（主席、公路局）保留。"""
    name = name.strip().strip("：:").strip()
    m = _GENERIC_SPEAKER_RE.match(name)
    return f"講者{int(m.group(1))}" if m else name


def _split_speaker(text: str) -> tuple[str, str, bool]:
    """「講者3（疑似）：內容」→（講者3, 內容, True）。沒有講者時回傳（"", 原文, False）。"""
    m = _SPEAKER_RE.match(text)
    if not m:
        return "", text.strip(), False
    speaker, body = m.group(1).strip(), m.group(2).strip()
    uncertain = speaker.endswith(_UNCERTAIN)
    return normalize_speaker(speaker.removesuffix(_UNCERTAIN)), body, uncertain


def _looks_like_speaker(text: str) -> bool:
    """時間碼後面只有短短的名稱（沒有標點），視為講者，內容在下一行。"""
    t = text.strip().removesuffix(_UNCERTAIN)
    return 0 < len(t) <= 20 and not _SENTENCE_PUNCT.search(t)


def _classify(line: str) -> tuple[str, float, str, bool]:
    """判斷一行：("head", 秒, 其餘文字, 有無小時)、("tail", 秒, 講者, 有無小時) 或 ("text", 0, 原文, False)。"""
    m = _HEAD_RE.match(line)
    if m:
        return "head", _seconds(*m.groups()[:4]), m.group(5).strip(), m.group(1) is not None
    m = _TAIL_RE.match(line)
    if m and _looks_like_speaker(m.group(1)):
        return "tail", _seconds(*m.groups()[1:5]), m.group(1).strip(), m.group(2) is not None
    return "text", 0.0, line, False


def _sanity_check(kinds: list[tuple[str, float, str, bool]], lines: list[str]) -> list[tuple[str, float, str]]:
    """避免把內容裡的時間（例如「1:20 的比例」）誤認成時間碼：

    - 檔案裡多數時間碼有「時」時，沒有「時」的視為內容；
    - 時間碼必須依序往後（倒退超過 1 秒的視為內容）。
    """
    stamps = [k for k in kinds if k[0] != "text"]
    need_hours = bool(stamps) and sum(1 for k in stamps if k[3]) > len(stamps) / 2
    out: list[tuple[str, float, str]] = []
    last = -1.0
    for (kind, sec, rest, hours), line in zip(kinds, lines, strict=True):
        if kind != "text" and ((need_hours and not hours) or sec < last - 1):
            out.append(("text", 0.0, line))
            continue
        if kind != "text":
            last = sec
        out.append((kind, sec, rest))
    return out


def parse_lines(lines: list[str]) -> tuple[list[RawSegment], bool]:
    """解析逐行逐字稿，回傳（片段, 是否有時間碼）。

    容錯：時間碼可在行首或行尾、可有可無括號、可只有分:秒；講者可在同一行（「講者：內容」）
    或時間碼後面單獨一行、內容在下一行；「演講者 1」等通用名稱統一成「講者1」；空行略過。
    """
    kinds = _sanity_check([_classify(ln) for ln in lines], lines)
    timed = any(k != "text" for k, _, _ in kinds)
    segments: list[RawSegment] = []
    if timed:
        for kind, sec, rest in kinds:
            if kind == "text":
                if segments and rest.strip():  # 時間碼下一行的內容，或換行接續
                    prev = segments[-1]
                    joined = prev.text + ("\n" if prev.text else "") + rest.strip()
                    segments[-1] = prev.model_copy(update={"text": joined})
                continue
            if kind == "tail" or (rest and ":" not in rest and "：" not in rest and _looks_like_speaker(rest)):
                speaker, body = rest, ""  # 講者單獨一行，內容在下一行
                uncertain = speaker.endswith(_UNCERTAIN)
                speaker = normalize_speaker(speaker.removesuffix(_UNCERTAIN))
            else:
                speaker, body, uncertain = _split_speaker(rest)
            segments.append(RawSegment(start=sec, end=sec, speaker=speaker or "講者", text=body,
                                       speaker_uncertain=uncertain))
        for i in range(len(segments) - 1):  # 結束時間＝下一段開始時間
            segments[i] = segments[i].model_copy(update={"end": max(segments[i].start, segments[i + 1].start)})
        return [s for s in segments if s.text.strip()], True
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
            segments, timed = parse_srt(_read_text(path)), True  # 與 txt 相同：UTF-8，再試 Big5
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
