"""摘要的 JSON Schema（給 LLM 結構化輸出用）。

為了同時相容 Anthropic 與 OpenAI 的嚴格模式，巢狀結構直接展開成固定層數（最多 3 層），
不使用遞迴 $ref；每個物件都列出全部欄位為必填、不允許額外欄位。
"""

from __future__ import annotations

from typing import Any

ROLES = ["一般敘述", "提問", "回答", "單位意見", "主席裁示"]


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_STR = {"type": "string"}
_IDS = {"type": "array", "items": {"type": "string"}, "description": "依據的逐字稿片段編號，例如 S0012"}


def _item(depth: int) -> dict[str, Any]:
    props: dict[str, Any] = {
        "role": {"type": "string", "enum": ROLES},
        "speaker": _STR,
        "text": _STR,
        "evidence": _IDS,
    }
    if depth < 3:
        props["children"] = {"type": "array", "items": _item(depth + 1)}
    return _obj(props)


def _node(depth: int) -> dict[str, Any]:
    props: dict[str, Any] = {"text": _STR}
    if depth < 3:
        props["children"] = {"type": "array", "items": _node(depth + 1)}
    return _obj(props)


def summary_schema() -> dict[str, Any]:
    """完整摘要的 JSON Schema。"""
    return _obj({
        "meeting_type": {"type": "string", "enum": ["topic", "qa", "agenda"]},
        "discussion_items": {"type": "array", "items": _obj({
            "title": _STR,
            "children": {"type": "array", "items": _STR},
            "evidence": _IDS,
        })},
        "conclusions": {"type": "array", "items": _obj({
            "title": _STR,
            "items": {"type": "array", "items": _item(1)},
        })},
        "action_items": {"type": "array", "items": _obj({
            "task": _STR, "owner": _STR, "due": _STR, "evidence": _IDS,
        })},
        "mindmap": _obj({"text": _STR, "children": {"type": "array", "items": _node(1)}}),
        "warnings": {"type": "array", "items": _STR},
    })
