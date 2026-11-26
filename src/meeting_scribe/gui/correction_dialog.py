"""AI 校正的確認視窗：列出文字校正與講者角色建議，使用者勾選（可修改建議內容）後才套用。"""

from __future__ import annotations

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
from meeting_scribe.postprocess.semantic import RoleSuggestion, Suggestions, TextFix

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
        self.tabs.addTab(self._with_tools(self.fix_table), f"文字校正（{len(suggestions.fixes)}）")
        self.tabs.addTab(self._with_tools(self.role_table), f"講者角色（{len(suggestions.roles)}）")
        if not suggestions.fixes and suggestions.roles:
            self.tabs.setCurrentIndex(1)
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

    def _with_tools(self, table: QTableWidget) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 6, 0, 0)
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
            table.item(row, 0).setCheckState(state)

    # ---- 結果 ----
    def chosen(self) -> tuple[list[TextFix], list[RoleSuggestion]]:
        """勾選的建議（含使用者在表格中的修改）。"""
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
