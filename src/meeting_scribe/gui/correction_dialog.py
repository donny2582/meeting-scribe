"""AI 校正的確認視窗：文字校正、講者角色、講者檢查三個分頁，使用者勾選（可修改建議內容）後才套用。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from meeting_scribe.gui.widgets import hint
from meeting_scribe.postprocess.semantic import (
    RoleSuggestion,
    SegmentReassign,
    SpeakerMerge,
    Suggestions,
    TextFix,
)

_READONLY = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
_EDITABLE = _READONLY | Qt.ItemFlag.ItemIsEditable


def _hms(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


class CorrectionDialog(QDialog):
    """「AI 校正」確認視窗。"""

    def __init__(self, parent: QWidget | None, doc: Any, suggestions: Suggestions) -> None:
        super().__init__(parent)
        self.doc = doc
        self.suggestions = suggestions
        self.setWindowTitle("AI 校正：請確認要套用的建議")
        self.resize(1000, 620)
        lay = QVBoxLayout(self)
        lay.addWidget(hint("AI 的建議不一定正確，請逐筆確認：取消勾選不要的建議；「修正後」與「建議名稱」可以直接修改。"
                           "按「套用勾選的項目」後才會修改逐字稿，套用後如果反悔，可以按「↶ 復原」。"))
        for note in suggestions.notes:
            lbl = QLabel(f"⚠ {note}")
            lbl.setWordWrap(True)
            lbl.setStyleSheet("color: #b26a00;")
            lay.addWidget(lbl)

        self.tabs = QTabWidget()
        self.fix_table = self._fix_table(suggestions.fixes)
        self.role_table = self._role_table(suggestions.roles)
        self.check_table = self._check_table(suggestions)
        self.tabs.addTab(self._with_tools(self.fix_table), f"文字校正（{len(suggestions.fixes)}）")
        self.tabs.addTab(self._with_tools(self.role_table), f"講者角色（{len(suggestions.roles)}）")
        self.tabs.addTab(self._with_tools(self.check_table, "AI 依對話邏輯檢查講者（聽不到聲音，只是建議）。"
                                          "「可能混了兩個人」只提醒，請在逐字稿表格中手動處理。"),
                         f"講者檢查（{suggestions.speaker_checks()}）")
        for i, n in enumerate((len(suggestions.fixes), len(suggestions.roles), suggestions.speaker_checks())):
            if n:
                self.tabs.setCurrentIndex(i)
                break
        lay.addWidget(self.tabs, 1)

        buttons = QDialogButtonBox()
        self.apply_button = buttons.addButton("套用勾選的項目", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("取消（都不套用）", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    # ---- 表格 ----
    @staticmethod
    def _check_item(checked: bool) -> QTableWidgetItem:
        item = QTableWidgetItem()
        item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        return item

    @staticmethod
    def _cell(text: str, editable: bool = False) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(_EDITABLE if editable else _READONLY)
        return item

    def _table(self, headers: list[str], stretch: list[int]) -> QTableWidget:
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().setVisible(False)
        t.setWordWrap(True)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = t.horizontalHeader()
        for col in range(len(headers)):
            mode = QHeaderView.ResizeMode.Stretch if col in stretch else QHeaderView.ResizeMode.ResizeToContents
            header.setSectionResizeMode(col, mode)
        return t

    def _fix_table(self, fixes: list[TextFix]) -> QTableWidget:
        t = self._table(["套用", "時間", "講者", "原文", "修正後（可修改）", "理由", "來源"], [3, 4, 5])
        t.setRowCount(len(fixes))
        for row, fix in enumerate(fixes):
            seg = self.doc.segments[fix.index]
            t.setItem(row, 0, self._check_item(True))
            t.setItem(row, 1, self._cell(_hms(seg.start) if self.doc.has_timecodes else f"第 {fix.index + 1} 段"))
            t.setItem(row, 2, self._cell(self.doc.display_name(seg.speaker)))
            t.setItem(row, 3, self._cell(fix.original))
            corrected = self._cell(fix.corrected, editable=True)
            corrected.setBackground(QColor("#e8f5e9"))
            t.setItem(row, 4, corrected)
            t.setItem(row, 5, self._cell(fix.reason))
            t.setItem(row, 6, self._cell(fix.source))
        t.resizeRowsToContents()
        return t

    def _role_table(self, roles: list[RoleSuggestion]) -> QTableWidget:
        t = self._table(["套用", "講者", "建議名稱（可修改）", "把握度", "依據", "理由"], [4, 5])
        t.setRowCount(len(roles))
        for row, role in enumerate(roles):
            t.setItem(row, 0, self._check_item(role.confidence in ("高", "中")))  # 低把握度預設不勾
            t.setItem(row, 1, self._cell(self.doc.display_name(role.speaker)))
            name = self._cell(role.name, editable=True)
            name.setBackground(QColor("#e8f5e9"))
            t.setItem(row, 2, name)
            t.setItem(row, 3, self._cell(role.confidence))
            evidence = []
            for i in role.evidence:
                seg = self.doc.segments[i]
                stamp = _hms(seg.start) if self.doc.has_timecodes else f"第 {i + 1} 段"
                evidence.append(f"{stamp}「{seg.text[:40]}{'…' if len(seg.text) > 40 else ''}」")
            t.setItem(row, 4, self._cell("\n".join(evidence) or "（沒有依據）"))
            t.setItem(row, 5, self._cell(role.reason))
        t.resizeRowsToContents()
        return t

    def _who(self, speaker: str) -> str:
        name = self.doc.display_name(speaker)
        return name if name == speaker else f"{name}（{speaker}）"

    def _stamp(self, index: int) -> str:
        seg = self.doc.segments[index]
        return _hms(seg.start) if self.doc.has_timecodes else f"第 {index + 1} 段"

    def _quote(self, index: int, limit: int = 60) -> str:
        text = self.doc.segments[index].text
        return text if len(text) <= limit else text[:limit] + "…"

    def _check_table(self, s: Suggestions) -> QTableWidget:
        """講者檢查：合併講者、改講者、可能混了兩個人（只提醒）。"""
        t = self._table(["套用", "類型", "時間", "內容", "建議", "把握度", "理由"], [3, 6])
        rows = len(s.merges) + len(s.reassigns) + len(s.mixed)
        t.setRowCount(rows)
        row = 0
        for m in s.merges:
            t.setItem(row, 0, self._check_item(m.confidence in ("高", "中")))
            t.setItem(row, 1, self._cell("合併講者"))
            t.setItem(row, 2, self._cell("、".join(self._stamp(i) for i in m.evidence) or "—"))
            t.setItem(row, 3, self._cell("\n".join(f"「{self._quote(i, 40)}」" for i in m.evidence) or "（沒有依據）"))
            t.setItem(row, 4, self._cell(f"{self._who(m.source)} 併入 {self._who(m.target)}"))
            t.setItem(row, 5, self._cell(m.confidence))
            t.setItem(row, 6, self._cell(m.reason))
            row += 1
        for r in s.reassigns:
            t.setItem(row, 0, self._check_item(r.confidence in ("高", "中")))
            t.setItem(row, 1, self._cell("改講者"))
            t.setItem(row, 2, self._cell(self._stamp(r.index)))
            t.setItem(row, 3, self._cell(self._quote(r.index)))
            t.setItem(row, 4, self._cell(f"{self._who(r.current)} → {self._who(r.suggested)}"))
            t.setItem(row, 5, self._cell(r.confidence))
            t.setItem(row, 6, self._cell(r.reason))
            row += 1
        for x in s.mixed:
            info = self._cell("只提醒")
            info.setForeground(QColor("#b26a00"))
            t.setItem(row, 0, info)
            t.setItem(row, 1, self._cell("可能混了兩個人"))
            t.setItem(row, 2, self._cell(self._stamp(x.index)))
            t.setItem(row, 3, self._cell(self._quote(x.index, 120)))
            t.setItem(row, 4, self._cell("請在逐字稿表格中手動處理"))
            t.setItem(row, 5, self._cell("—"))
            t.setItem(row, 6, self._cell(x.reason))
            row += 1
        t.resizeRowsToContents()
        return t

    def _with_tools(self, table: QTableWidget, note: str = "") -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 6, 0, 0)
        if note:
            v.addWidget(hint(note))
        row = QHBoxLayout()
        for text, state in (("全部勾選", Qt.CheckState.Checked), ("全部取消", Qt.CheckState.Unchecked)):
            b = QPushButton(text)
            b.clicked.connect(lambda _c=False, s=state: self._set_all(table, s))
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        if table.rowCount() == 0:
            v.addWidget(hint("沒有建議。"))
        v.addWidget(table, 1)
        return w

    @staticmethod
    def _set_all(table: QTableWidget, state: Qt.CheckState) -> None:
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:  # 「只提醒」的列沒有勾選框
                item.setCheckState(state)

    # ---- 結果 ----
    def _checked(self, table: QTableWidget, row: int) -> bool:
        item = table.item(row, 0)
        return bool(item.flags() & Qt.ItemFlag.ItemIsUserCheckable) and item.checkState() == Qt.CheckState.Checked

    def chosen_speaker_checks(self) -> tuple[list[SpeakerMerge], list[SegmentReassign]]:
        """勾選的講者檢查建議（合併講者、改講者）。"""
        s = self.suggestions
        merges = [m for row, m in enumerate(s.merges) if self._checked(self.check_table, row)]
        offset = len(s.merges)
        reassigns = [r for row, r in enumerate(s.reassigns) if self._checked(self.check_table, offset + row)]
        return merges, reassigns

    def chosen(self) -> tuple[list[TextFix], list[RoleSuggestion]]:
        """勾選的文字校正與講者角色建議（含使用者在表格中的修改）。"""
        fixes = []
        for row, fix in enumerate(self.suggestions.fixes):
            if self.fix_table.item(row, 0).checkState() == Qt.CheckState.Checked:
                text = self.fix_table.item(row, 4).text().strip()
                if text and text != fix.original:
                    fixes.append(TextFix(fix.index, fix.original, text, fix.reason, fix.source))
        roles = []
        for row, role in enumerate(self.suggestions.roles):
            if self.role_table.item(row, 0).checkState() == Qt.CheckState.Checked:
                name = self.role_table.item(row, 2).text().strip()
                if name:
                    roles.append(RoleSuggestion(role.speaker, name, role.confidence, role.evidence, role.reason))
        return fixes, roles


class CorrectionSourceDialog(QDialog):
    """按下「AI 校正…」時先選擇：雲端比對，或使用已有的校正檔。"""

    CLOUD, FILES = "cloud", "files"

    def __init__(self, parent: QWidget | None, folder: Any) -> None:
        from PySide6.QtWidgets import QButtonGroup, QFormLayout, QMessageBox, QRadioButton

        from meeting_scribe.gui.widgets import FilePicker
        from meeting_scribe.postprocess.ai_review import CORRECTED_FILE, DRAFT_FILE

        super().__init__(parent)
        self._message = QMessageBox
        self.setWindowTitle("AI 校正")
        self.resize(640, 320)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<b>要用哪一種方式校正？</b>"))
        self.cloud = QRadioButton("① 雲端比對：由 AI 檢查錯字與講者（會產生費用，約 US$0.2–0.5）")
        self.files = QRadioButton("② 已有現成校正檔：選擇逐字稿草稿與之前產生的 AI 校正檔，直接比對（不花費用）")
        group = QButtonGroup(self)
        for rb in (self.cloud, self.files):
            group.addButton(rb)
        self.cloud.setChecked(True)
        lay.addWidget(self.cloud)
        lay.addWidget(hint("跑完後列出建議讓您確認；套用後在表格以淡橘色標記，並在這場會議的資料夾輸出"
                           f"「{CORRECTED_FILE}」與「{DRAFT_FILE}」。"))
        lay.addWidget(self.files)
        self.file_box = QWidget()
        form = QFormLayout(self.file_box)
        txt = "逐字稿 (*.txt *.srt *.docx *.json);;所有檔案 (*.*)"
        self.draft = FilePicker("open", txt, "逐字稿草稿（比對基準）", "選擇逐字稿草稿")
        self.corrected = FilePicker("open", txt, "逐字稿草稿_AI校正", "選擇 AI 校正檔")
        folder = Path(folder) if folder else None
        if folder is not None:
            for picker, name in ((self.draft, DRAFT_FILE), (self.corrected, CORRECTED_FILE)):
                if (folder / name).is_file():
                    picker.set_path(folder / name)
        form.addRow("逐字稿草稿：", self.draft)
        form.addRow("AI 校正檔：", self.corrected)
        form.addRow("", hint("以逐字稿草稿為主，依時間碼對齊校正檔（容許 ±2 秒誤差）；"
                             "校正檔裡找不到對應、或沒有講者的列會標成淡綠色。會以對齊結果取代目前的表格內容（可復原）。"))
        lay.addWidget(self.file_box)
        self.cloud.toggled.connect(lambda _on: self.file_box.setEnabled(self.files.isChecked()))
        self.file_box.setEnabled(False)
        lay.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("確定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def mode(self) -> str:
        return self.FILES if self.files.isChecked() else self.CLOUD

    def paths(self) -> tuple[Any, Any]:
        return self.draft.path(), self.corrected.path()

    def _accept(self) -> None:
        if self.mode() == self.FILES:
            draft, corrected = self.paths()
            missing = [label for label, p in (("逐字稿草稿", draft), ("AI 校正檔", corrected))
                       if p is None or not p.is_file()]
            if missing:
                self._message.information(self, "AI 校正", "請選擇：" + "、".join(missing))
                return
        self.accept()