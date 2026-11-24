"""心智圖 XMind 檔（SPEC 9.4）：直接產生 .xmind（zip：content.json、metadata.json、manifest.json），不依賴第三方套件。

結構與 XMind 2020 以後版本（含 XMind 2024 免費版）使用的 JSON 格式相同：
一個畫布（sheet）、根主題由左往右展開（logic chart），第 1 層分支沿用 PNG 的顏色。
"""

from __future__ import annotations

import json
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from meeting_scribe.models import MindNode
from meeting_scribe.render.mindmap_png import PALETTE

STRUCTURE = "org.xmind.ui.logic.right"


def _topic(node: MindNode, depth: int, color: str | None = None) -> dict[str, Any]:
    topic: dict[str, Any] = {"id": uuid.uuid4().hex, "class": "topic", "title": node.text.strip() or "（空白）"}
    if depth == 0:
        topic["structureClass"] = STRUCTURE
    if color and depth == 1:
        topic["style"] = {"id": uuid.uuid4().hex, "properties": {"svg:fill": color, "fo:color": "#FFFFFF"}}
    if node.children:
        topic["children"] = {"attached": [
            _topic(c, depth + 1, PALETTE[i % len(PALETTE)] if depth == 0 else color)
            for i, c in enumerate(node.children)
        ]}
    return topic


def build_content(root: MindNode, sheet_title: str = "會議心智圖") -> list[dict[str, Any]]:
    """content.json 的內容。"""
    return [{
        "id": uuid.uuid4().hex,
        "class": "sheet",
        "title": sheet_title,
        "rootTopic": _topic(root, 0),
        "topicPositioning": "fixed",
    }]


def render_mindmap_xmind(root: MindNode, path: Path) -> Path:
    """寫出 .xmind 檔。"""
    content = build_content(root)
    metadata = {"creator": {"name": "MeetingScribe", "version": "1.0"},
                "activeSheetId": content[0]["id"], "dataStructureVersion": "2", "created": int(time.time() * 1000)}
    manifest = {"file-entries": {"content.json": {}, "metadata.json": {}}}
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("content.json", json.dumps(content, ensure_ascii=False))
        z.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False))
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
    return path
