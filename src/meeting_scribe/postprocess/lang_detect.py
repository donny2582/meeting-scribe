"""片段語言判定（SPEC 6.3）。

依字元組成判斷，規則與門檻全部來自 ``assets/languages.toml``：

- 每個字元系統（漢字、假名、韓文字母、拉丁字母）以「字」或「單字」為計數單位。
- 各語言的比例＝該語言字元系統的單位數 ÷ 片段總單位數；超過門檻即視為該外語。
- 設有 ``absorbs`` 的語言（日文）在判定成立時，片段中的漢字也算入該語言；
  因此只有漢字、沒有假名的片段會判定為中文。
- 兩種以上語言比例都超過 ``mixed_threshold`` 且至少一種外語成立時，判定為「混合」。
- 引擎有回傳語言時，以字元規則為準；字元規則無法判斷（例如只有數字）或該語言沒有字元規則
  （例如台語）時，才採用引擎的判定。
"""

from __future__ import annotations

import logging
import tomllib
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from meeting_scribe.config import assets_dir

log = logging.getLogger(__name__)

UNKNOWN = "unknown"
MIXED = "mixed"


@dataclass(frozen=True)
class Script:
    """字元系統。"""

    name: str
    unit: str  # "char" 或 "word"
    ranges: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class LanguageRule:
    """單一語言的設定。"""

    code: str
    name: str
    stt_locale: str
    scripts: tuple[str, ...]
    absorbs: tuple[str, ...] = ()
    threshold: float = 1.0
    base: bool = False
    selectable: bool = True
    fillers: tuple[str, ...] = ()
    repeat_collapse: tuple[str, ...] = ()


@dataclass
class LanguageRegistry:
    """語言清單（由 languages.toml 載入）。"""

    scripts: dict[str, Script]
    languages: dict[str, LanguageRule]
    mixed_threshold: float = 0.3
    _char_cache: dict[str, str | None] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, path: Path | None = None) -> LanguageRegistry:
        """從 TOML 檔載入語言清單。"""
        with open(path or assets_dir() / "languages.toml", "rb") as f:
            data = tomllib.load(f)
        scripts = {
            name: Script(
                name=name,
                unit=spec.get("unit", "char"),
                ranges=tuple(
                    (int(r.split("-")[0], 16), int(r.split("-")[1], 16)) for r in spec.get("ranges", [])
                ),
            )
            for name, spec in data.get("scripts", {}).items()
        }
        languages = {
            code: LanguageRule(
                code=code,
                name=spec.get("name", code),
                stt_locale=spec.get("stt_locale", code),
                scripts=tuple(spec.get("scripts", [])),
                absorbs=tuple(spec.get("absorbs", [])),
                threshold=float(spec.get("threshold", 1.0)),
                base=bool(spec.get("base", False)),
                selectable=bool(spec.get("selectable", True)),
                fillers=tuple(spec.get("fillers", [])),
                repeat_collapse=tuple(spec.get("repeat_collapse", [])),
            )
            for code, spec in data.get("languages", {}).items()
        }
        mixed = float(data.get("detection", {}).get("mixed_threshold", 0.3))
        return cls(scripts=scripts, languages=languages, mixed_threshold=mixed)

    @property
    def base_code(self) -> str:
        """主要語言代碼（中文）。"""
        for rule in self.languages.values():
            if rule.base:
                return rule.code
        return "zh"

    def script_of(self, ch: str) -> str | None:
        """回傳字元所屬的字元系統名稱。"""
        if ch in self._char_cache:
            return self._char_cache[ch]
        cp = ord(ch)
        found = None
        for script in self.scripts.values():
            if any(lo <= cp <= hi for lo, hi in script.ranges):
                found = script.name
                break
        self._char_cache[ch] = found
        return found

    def locales_for(self, codes: list[str]) -> list[str]:
        """把語言代碼轉成 STT 地區設定清單。"""
        return [self.languages[c].stt_locale for c in codes if c in self.languages]

    def code_from_locale(self, locale: str | None) -> str | None:
        """把引擎回傳的地區設定（例：zh-TW、en、ja-JP）轉成語言代碼。"""
        if not locale:
            return None
        loc = locale.lower()
        for rule in self.languages.values():
            if loc == rule.stt_locale.lower() or loc == rule.code or loc.split("-")[0] == rule.code:
                return rule.code
        if loc.startswith(("cmn", "yue")):
            return "zh"
        return None


@lru_cache(maxsize=1)
def default_registry() -> LanguageRegistry:
    """載入預設語言清單（快取）。"""
    return LanguageRegistry.load()


def count_units(text: str, registry: LanguageRegistry) -> dict[str, int]:
    """計算各字元系統的單位數（字或單字）。"""
    counts = {name: 0 for name in registry.scripts}
    prev: str | None = None
    for ch in unicodedata.normalize("NFKC", text):
        script = registry.script_of(ch)
        if script is not None and (registry.scripts[script].unit == "char" or script != prev):
            counts[script] += 1
        # 撇號與連字號不中斷英文單字（例：don't、e-mail）
        if script is None and ch in "'-’" and prev is not None:
            continue
        prev = script
    return counts


@dataclass
class Detection:
    """語言判定結果。"""

    lang: str
    shares: dict[str, float]
    passed: list[str]
    engine_lang: str | None = None
    disagree: bool = False


def detect(text: str, registry: LanguageRegistry | None = None, engine_locale: str | None = None) -> Detection:
    """判定片段語言，回傳詳細結果。"""
    reg = registry or default_registry()
    engine_code = reg.code_from_locale(engine_locale)
    units = count_units(text, reg)
    total = sum(units.values())
    if total == 0:
        return Detection(engine_code or UNKNOWN, {}, [], engine_code)

    base = reg.base_code
    passed: list[str] = []
    absorbed: set[str] = set()
    for rule in reg.languages.values():
        if rule.base or not rule.scripts:
            continue
        own = sum(units.get(s, 0) for s in rule.scripts) / total
        if own > rule.threshold:
            passed.append(rule.code)
            absorbed.update(rule.absorbs)

    shares: dict[str, float] = {}
    for rule in reg.languages.values():
        if not rule.scripts:
            continue
        scripts = list(rule.scripts)
        if rule.code in passed:
            scripts += list(rule.absorbs)
        if rule.base:
            scripts = [s for s in scripts if s not in absorbed]
        shares[rule.code] = sum(units.get(s, 0) for s in set(scripts)) / total

    over_mixed = [c for c, v in shares.items() if v > reg.mixed_threshold]
    if passed and len(over_mixed) >= 2:
        lang = MIXED
    elif passed:
        lang = max(passed, key=lambda c: shares.get(c, 0.0))
    elif shares.get(base, 0.0) > 0:
        lang = base
    else:
        lang = UNKNOWN

    disagree = False
    if engine_code is not None and engine_code != lang:
        engine_rule = reg.languages.get(engine_code)
        if lang == UNKNOWN or (engine_rule is not None and not engine_rule.scripts):
            # 字元規則無法判斷，或引擎判定的是沒有字元規則的語言（台語）
            lang = engine_code
        elif lang != MIXED:
            disagree = True
            log.debug("語言判定不一致：引擎=%s，字元規則=%s（以字元規則為準）", engine_code, lang)
    return Detection(lang, shares, passed, engine_code, disagree)


def detect_language(text: str, registry: LanguageRegistry | None = None, engine_locale: str | None = None) -> str:
    """判定片段語言，回傳語言代碼（zh／en／ja／ko／mixed／nan／unknown）。"""
    return detect(text, registry, engine_locale).lang
