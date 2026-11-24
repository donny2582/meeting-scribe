"""心智圖 PNG（SPEC 9.4）：由左往右展開的樹狀圖（logic chart），用 Pillow 自行繪製。

- 根節點＝會議名稱；第 1 層的分支用不同顏色；節點文字超過 20 字自動換行。
- 字型：微軟正黑體；找不到時改用 assets\\fonts 內附的 Noto Sans TC，再找不到才用 Pillow 內建字型。
- 寬度至少 2400 px（可清楚放進簡報）；節點多時自動加高。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from meeting_scribe.config import assets_dir
from meeting_scribe.models import MindNode

MIN_WIDTH = 2400
WRAP = 20
PALETTE = ["#1f6feb", "#d1495b", "#2a9d8f", "#e9a23b", "#7b4fd1", "#3a86a8", "#c2551f", "#5a8f29",
           "#b83280", "#4a6fa5"]
FONT_SIZES = [46, 34, 28, 26]  # 根、第 1、2、3 層
PAD = [(36, 22), (26, 16), (20, 12), (18, 10)]
H_GAP, V_GAP, MARGIN = 110, 18, 70


def _font_paths(bold: bool) -> list[Path]:
    win = Path("C:/Windows/Fonts")
    names = ["msjhbd.ttc", "msjh.ttc"] if bold else ["msjh.ttc", "msjhbd.ttc"]
    return [win / n for n in names] + [assets_dir() / "fonts" / "NotoSansTC-Regular.otf"]


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _font_paths(bold):
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default(size)


_NO_LINE_START = "）」』】〉》，。、；：！？,.;:!?)]"


def wrap(text: str, width: int = WRAP) -> list[str]:
    """超過 width 字自動換行（中文逐字計算）；保留原有的換行，行首不放結束標點（移到上一行行尾）。"""
    lines: list[str] = []
    for part in (text.strip() or "（空白）").splitlines():
        part = part.strip()
        chunk = [part[i:i + width] for i in range(0, len(part), width)] or [""]
        for piece in chunk:
            while lines and piece and piece[0] in _NO_LINE_START:
                lines[-1] += piece[0]
                piece = piece[1:]
            if piece:
                lines.append(piece)
    return lines or ["（空白）"]


@dataclass
class _Box:
    node: MindNode
    depth: int
    lines: list[str]
    w: int = 0
    h: int = 0
    x: int = 0
    y: int = 0
    color: str = PALETTE[0]
    children: list[_Box] = field(default_factory=list)
    span: int = 0  # 子樹高度


def _build(node: MindNode, depth: int, color: str, draw: ImageDraw.ImageDraw) -> _Box:
    lvl = min(depth, 3)
    font = load_font(FONT_SIZES[lvl], bold=depth <= 1)
    lines = wrap(node.text)
    widths = [draw.textbbox((0, 0), ln, font=font)[2] for ln in lines]
    line_h = int(FONT_SIZES[lvl] * 1.35)
    px, py = PAD[lvl]
    box = _Box(node, depth, lines, max(widths) + 2 * px, line_h * len(lines) + 2 * py, color=color)
    for i, child in enumerate(node.children):
        c = PALETTE[i % len(PALETTE)] if depth == 0 else color
        box.children.append(_build(child, depth + 1, c, draw))
    return box


def _span(box: _Box) -> int:
    if not box.children:
        box.span = box.h + V_GAP
    else:
        box.span = max(box.h + V_GAP, sum(_span(c) for c in box.children))
    return box.span


def _place(box: _Box, top: int, col_x: list[int]) -> None:
    box.x = col_x[box.depth]
    box.y = top + (box.span - box.h) // 2
    child_total = sum(c.span for c in box.children)
    cur = top + (box.span - child_total) // 2
    for c in box.children:
        _place(c, cur, col_x)
        cur += c.span


def _walk(box: _Box) -> list[_Box]:
    out = [box]
    for c in box.children:
        out += _walk(c)
    return out


def _tint(hex_color: str, ratio: float) -> tuple[int, int, int]:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return (int(r + (255 - r) * ratio), int(g + (255 - g) * ratio), int(b + (255 - b) * ratio))


def _curve(draw: ImageDraw.ImageDraw, p0: tuple[int, int], p3: tuple[int, int], color: str, width: int) -> None:
    """三次貝茲曲線（以折線近似）。"""
    mid = (p0[0] + p3[0]) / 2
    p1, p2 = (mid, p0[1]), (mid, p3[1])
    pts = []
    for i in range(25):
        t = i / 24
        x = (1 - t) ** 3 * p0[0] + 3 * (1 - t) ** 2 * t * p1[0] + 3 * (1 - t) * t ** 2 * p2[0] + t ** 3 * p3[0]
        y = (1 - t) ** 3 * p0[1] + 3 * (1 - t) ** 2 * t * p1[1] + 3 * (1 - t) * t ** 2 * p2[1] + t ** 3 * p3[1]
        pts.append((x, y))
    draw.line(pts, fill=color, width=width, joint="curve")


def render_mindmap_png(root: MindNode, path: Path, min_width: int = MIN_WIDTH) -> Path:
    """畫出心智圖並存成 PNG。"""
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    tree = _build(root, 0, "#16325c", probe)
    boxes = _walk(tree)
    depth = max(b.depth for b in boxes)
    col_w = [max(b.w for b in boxes if b.depth == d) for d in range(depth + 1)]
    natural = 2 * MARGIN + sum(col_w) + H_GAP * depth
    gap = H_GAP + (max(0, min_width - natural) // depth if depth else 0)  # 不足 2400 px 時拉開欄距
    col_x = [MARGIN]
    for d in range(1, depth + 1):
        col_x.append(col_x[-1] + col_w[d - 1] + gap)
    width = max(min_width, col_x[-1] + col_w[-1] + MARGIN)
    _span(tree)
    height = max(900, tree.span + 2 * MARGIN)
    _place(tree, MARGIN, col_x)

    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    for b in boxes:  # 先畫連線，再畫節點
        for c in b.children:
            _curve(draw, (b.x + b.w, b.y + b.h // 2), (c.x, c.y + c.h // 2), c.color, 5 if b.depth == 0 else 3)
    for b in boxes:
        lvl = min(b.depth, 3)
        font = load_font(FONT_SIZES[lvl], bold=b.depth <= 1)
        rect = [b.x, b.y, b.x + b.w, b.y + b.h]
        if b.depth <= 1:
            fill, outline, text_color = b.color, b.color, "white"
        elif b.depth == 2:
            fill, outline, text_color = "white", b.color, "#1f2328"
        else:
            fill, outline, text_color = _tint(b.color, 0.85), _tint(b.color, 0.4), "#1f2328"
        draw.rounded_rectangle(rect, radius=16 if b.depth <= 1 else 12, fill=fill, outline=outline, width=3)
        px, py = PAD[lvl]
        line_h = int(FONT_SIZES[lvl] * 1.35)
        for i, line in enumerate(b.lines):
            draw.text((b.x + px, b.y + py + i * line_h), line, font=font, fill=text_color)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG", optimize=True)
    return path
