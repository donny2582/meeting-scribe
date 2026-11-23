"""語言判定：中、英、日、韓、混合（SPEC 6.3）。"""

import pytest

from meeting_scribe.postprocess.lang_detect import LanguageRegistry, count_units, detect, detect_language


@pytest.fixture(scope="module")
def reg() -> LanguageRegistry:
    return LanguageRegistry.load()


@pytest.mark.parametrize(
    "text,expected",
    [
        ("好，時間到了，我們開始今天的會議。", "zh"),
        ("我們用LiDAR量測標線的夜間反光度", "zh"),  # 中文夾雜術語不視為外語
        ("這個 ADAS 跟 ETC 的資料要再確認", "zh"),
        ("Our team currently holds a patent on using LiDAR.", "en"),
        ("高齢ドライバーの免許返納制度については、日本では1998年から始まりました。", "ja"),
        ("警察庁と国土交通省が担当します", "ja"),  # 假名 > 20%，漢字歸入日文
        ("警察庁と国土交通省", "zh"),  # 假名只占 11%，依規則視為中文
        ("警察廳國土交通省", "zh"),  # 只有漢字、沒有假名視為中文
        ("한국의 구간단속은 경찰청에서 운영하고 있습니다.", "ko"),
        ("這個구간단속制度", "mixed"),
        ("OK 那我們 next slide please", "mixed"),
        ("1998 2024", "unknown"),
    ],
)
def test_detect(reg: LanguageRegistry, text: str, expected: str) -> None:
    assert detect_language(text, reg) == expected


def test_english_word_counting(reg: LanguageRegistry) -> None:
    units = count_units("don't stop 停", reg)
    assert units["latin"] == 2
    assert units["han"] == 1


def test_engine_lang_used_when_rule_unknown(reg: LanguageRegistry) -> None:
    assert detect_language("2024", reg, "en-US") == "en"


def test_rule_wins_on_disagreement(reg: LanguageRegistry) -> None:
    d = detect("我們開始開會", reg, "ja-JP")
    assert d.lang == "zh"
    assert d.disagree


def test_engine_taiwanese_kept(reg: LanguageRegistry) -> None:
    assert detect_language("今仔日天氣真好", reg, "nan-TW") == "nan"


def test_locales(reg: LanguageRegistry) -> None:
    assert reg.locales_for(["zh", "en", "ja", "ko"]) == ["zh-TW", "en-US", "ja-JP", "ko-KR"]
    assert reg.code_from_locale("zh-TW") == "zh"
    assert reg.code_from_locale("en") == "en"
    assert reg.base_code == "zh"
