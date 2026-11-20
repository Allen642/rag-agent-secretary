"""UTF-8 安全截断工具的测试。"""

from __future__ import annotations

from secretary.text_utils import DEFAULT_TRUNCATE_SUFFIX, truncate_utf8


def test_short_text_is_returned_unchanged() -> None:
    assert truncate_utf8("你好，世界") == "你好，世界"
    assert truncate_utf8("", limit=10) == ""


def test_over_limit_text_is_truncated_with_suffix() -> None:
    text = "中" * 3000

    truncated = truncate_utf8(text, 100)

    assert len(truncated.encode("utf-8")) <= 100
    assert truncated.endswith(DEFAULT_TRUNCATE_SUFFIX)


def test_multibyte_boundary_is_never_split() -> None:
    # 每个汉字 3 字节；97 不是 3 的倍数，必然落在字中间
    truncated = truncate_utf8("中" * 3000, 97)

    assert truncated.startswith("中")
    assert "\ufffd" not in truncated
    assert len(truncated.encode("utf-8")) <= 97


def test_limit_not_larger_than_suffix_still_safe() -> None:
    # 后缀 “……” 本身是 6 字节；limit 更小时只能安全截断后缀
    result = truncate_utf8("中" * 100, 4)

    assert len(result.encode("utf-8")) <= 4
    assert result == "…"
    assert "\ufffd" not in result
