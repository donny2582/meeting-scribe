"""Word 文件共用設定：臺灣繁體中文的語言與字型，讓手機、平板、Mac 的 Word 也能正確顯示中文。

python-docx 內建的空白範本來自日文版 Word：預設語言是英文（eastAsia=en-US）、
字型表只有日文字型（ＭＳ 明朝、ＭＳ ゴシック），也沒有「標楷體」。電腦版 Word 有標楷體時看不出問題，
但手機上沒有標楷體，Word 依字型表與語言找替代字型時可能選錯，中文就變成方框或亂碼。

這裡比照使用者提供的參考文件（電腦版 Word 存檔）：
- 預設語言：英文 en-US、東亞文字 zh-TW（臺灣繁體中文）
- 預設字型：西文 Times New Roman、中文 標楷體（不使用佈景主題字型）
- 字型表加入「標楷體」（英文名 DFKai-SB、字集 Big5）與「新細明體」（PMingLiU），移除日文字型
"""

from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.document import Document as DocumentObject
from docx.oxml.ns import qn

CJK_FONT = "標楷體"
LATIN_FONT = "Times New Roman"

#: 字型表的條目（參數取自 Word 存檔的參考文件）
_FONT_ENTRIES = {
    "Times New Roman": (
        '<w:font w:name="Times New Roman"><w:panose1 w:val="02020603050405020304"/><w:charset w:val="00"/>'
        '<w:family w:val="roman"/><w:pitch w:val="variable"/><w:sig w:usb0="E0002EFF" w:usb1="C000785B" '
        'w:usb2="00000009" w:usb3="00000000" w:csb0="000001FF" w:csb1="00000000"/></w:font>'),
    "標楷體": (
        '<w:font w:name="標楷體"><w:altName w:val="DFKai-SB"/><w:panose1 w:val="03000509000000000000"/>'
        '<w:charset w:val="88"/><w:family w:val="script"/><w:pitch w:val="fixed"/><w:sig w:usb0="00000003" '
        'w:usb1="080E0000" w:usb2="00000016" w:usb3="00000000" w:csb0="00100001" w:csb1="00000000"/></w:font>'),
    "新細明體": (
        '<w:font w:name="新細明體"><w:altName w:val="PMingLiU"/><w:panose1 w:val="02020500000000000000"/>'
        '<w:charset w:val="88"/><w:family w:val="roman"/><w:pitch w:val="variable"/><w:sig w:usb0="A00002FF" '
        'w:usb1="28CFFCFA" w:usb2="00000016" w:usb3="00000000" w:csb0="00100001" w:csb1="00000000"/></w:font>'),
}
_JAPANESE_FONTS = ("ＭＳ 明朝", "ＭＳ ゴシック")


def new_document(template: Path | None = None) -> DocumentObject:
    """建立 Word 文件並套用臺灣繁體中文的語言與字型設定。"""
    d = Document(str(template)) if template else Document()
    apply_zh_tw(d)
    return d


def _set_fonts(fonts: object) -> None:
    for theme_attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        fonts.attrib.pop(qn(theme_attr), None)  # type: ignore[attr-defined]
    for attr, value in (("w:ascii", LATIN_FONT), ("w:hAnsi", LATIN_FONT), ("w:cs", LATIN_FONT),
                        ("w:eastAsia", CJK_FONT)):
        fonts.set(qn(attr), value)  # type: ignore[attr-defined]


def apply_zh_tw(d: DocumentObject) -> None:
    """把文件的預設語言、字型與字型表改成臺灣繁體中文（見模組說明）。"""
    styles = d.styles.element
    defaults = styles.find(qn("w:docDefaults"))
    if defaults is None:
        defaults = styles.makeelement(qn("w:docDefaults"), {})
        styles.insert(0, defaults)
    rpr_default = defaults.find(qn("w:rPrDefault"))
    if rpr_default is None:
        rpr_default = defaults.makeelement(qn("w:rPrDefault"), {})
        defaults.insert(0, rpr_default)
    rpr = rpr_default.find(qn("w:rPr"))
    if rpr is None:
        rpr = rpr_default.makeelement(qn("w:rPr"), {})
        rpr_default.append(rpr)
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.insert(0, fonts)
    _set_fonts(fonts)
    fonts.set(qn("w:hint"), "eastAsia")  # 引號、刪節號等全形／半形共用的符號也用中文字型
    lang = rpr.find(qn("w:lang"))
    if lang is None:
        lang = rpr.makeelement(qn("w:lang"), {})
        rpr.append(lang)
    lang.set(qn("w:val"), "en-US")
    lang.set(qn("w:eastAsia"), "zh-TW")
    lang.set(qn("w:bidi"), "ar-SA")
    # 標題等樣式原本用佈景主題字型（對應到日文、簡體字型），一律改成標楷體／Times New Roman
    for f in styles.iter(qn("w:rFonts")):
        if f is not fonts and f.get(qn("w:eastAsiaTheme")):
            _set_fonts(f)
    _fix_font_table(d)


def _fix_font_table(d: DocumentObject) -> None:
    for rel in d.part.rels.values():
        if rel.reltype.endswith("/fontTable") and not rel.is_external:
            part = rel.target_part
            xml = part.blob.decode("utf-8")
            for name in _JAPANESE_FONTS:
                xml = re.sub(rf'<w:font w:name="{re.escape(name)}">.*?</w:font>', "", xml, flags=re.DOTALL)
            for name, entry in _FONT_ENTRIES.items():
                xml = re.sub(rf'<w:font w:name="{re.escape(name)}">.*?</w:font>', "", xml, flags=re.DOTALL)
                xml = xml.replace("</w:fonts>", entry + "</w:fonts>")
            part._blob = xml.encode("utf-8")
            return
