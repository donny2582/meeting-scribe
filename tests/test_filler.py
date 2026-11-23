"""贅字規則層與子序列驗證（SPEC 6.4）。"""

from meeting_scribe.postprocess.filler import is_subsequence, remove_fillers_rule, strip_punct

ZH_FILLERS = ["呃", "嗯", "欸", "那個", "就是說"]


def test_remove_zh_fillers() -> None:
    src = "呃，那個，我們下週要提交報告。"
    out = remove_fillers_rule(src, ZH_FILLERS, ["對"])
    assert out == "我們下週要提交報告。"
    assert is_subsequence(out, src)


def test_collapse_repeats() -> None:
    assert remove_fillers_rule("對對對，就照這樣辦", [], ["對"]) == "對，就照這樣辦"
    assert remove_fillers_rule("對，對，對，沒問題", [], ["對"]) == "對，沒問題"


def test_english_fillers_word_boundary() -> None:
    out = remove_fillers_rule("Um, we, uh, have the umbrella data.", ["uh", "um"], [])
    assert "umbrella" in out
    assert out.lower().startswith("we")


def test_subsequence_rejects_insert_and_reorder() -> None:
    src = "我們下週提交報告"
    assert is_subsequence("我們提交報告", src)
    assert not is_subsequence("我們下週一提交報告", src)  # 新增字
    assert not is_subsequence("下週我們提交報告", src)  # 調換順序


def test_subsequence_ignores_punctuation() -> None:
    assert is_subsequence("我們，下週。提交！", "我們下週提交")


def test_strip_punct() -> None:
    assert strip_punct("你好， world！") == "你好world"
