"""依據片段驗證（SPEC 7.3）。

- evidence 中的每一個片段編號都必須存在；不存在的移除，移除後清單變成空的，條目標記「（待查證）」。
- 主席未明確裁示、依據片段的講者不確定或辨識信心低時，也標記「（待查證）」並說明原因。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from meeting_scribe.models import ActionItem, Summary, SummaryItem


@dataclass
class ValidationReport:
    """驗證統計（顯示在執行紀錄）。"""

    items: int = 0
    removed_ids: int = 0
    pending: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def mark(self, reason: str) -> None:
        self.pending += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


def _clean_ids(ids: list[str], known: set[str], report: ValidationReport) -> list[str]:
    kept = [i.strip().upper() for i in ids if i.strip().upper() in known]
    report.removed_ids += len(ids) - len(kept)
    return list(dict.fromkeys(kept))


def validate_summary(summary: Summary, known_ids: set[str], uncertain_ids: set[str] | None = None) -> ValidationReport:
    """就地修正 summary，回傳統計。uncertain_ids：講者不確定或需要重聽的片段。"""
    report = ValidationReport()
    uncertain = uncertain_ids or set()

    def check_item(item: SummaryItem) -> None:
        report.items += 1
        item.evidence = _clean_ids(item.evidence, known_ids, report)
        if not item.evidence:
            item.pending, item.pending_reason = True, "找不到依據的逐字稿片段"
            report.mark(item.pending_reason)
        elif "未明確裁示" in item.text:
            item.pending, item.pending_reason = True, "主席未明確裁示"
            report.mark(item.pending_reason)
        elif set(item.evidence) <= uncertain:
            item.pending, item.pending_reason = True, "依據片段的講者辨識不確定，請重聽確認"
            report.mark(item.pending_reason)
        for child in item.children:
            check_item(child)

    for section in summary.conclusions:
        for item in section.items:
            check_item(item)
    for d in summary.discussion_items:
        d.evidence = _clean_ids(d.evidence, known_ids, report)
    for a in summary.action_items:
        _check_action(a, known_ids, report)
    return report


def _check_action(a: ActionItem, known: set[str], report: ValidationReport) -> None:
    report.items += 1
    a.evidence = _clean_ids(a.evidence, known, report)
    if not a.evidence:
        a.pending, a.pending_reason = True, "找不到依據的逐字稿片段"
        report.mark(a.pending_reason)
