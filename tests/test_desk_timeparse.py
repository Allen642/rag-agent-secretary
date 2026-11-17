"""自然语言时间解析测试。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from secretary.desk import parse_natural

# 2026-11-18 是星期三，固定下来让「下周一」等断言稳定
NOW = datetime(2026, 11, 18, 14, 30)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 绝对时间：先走 parse_timestamp
        ("2026-11-20 09:00", datetime(2026, 11, 20, 9, 0)),
        ("2026-11-20T09:30:00", datetime(2026, 11, 20, 9, 30)),
        ("2026-11-20", datetime(2026, 11, 20, 18, 0)),
        # 日期词
        ("今天", datetime(2026, 11, 18, 18, 0)),
        ("明天", datetime(2026, 11, 19, 18, 0)),
        ("明天下午3点", datetime(2026, 11, 19, 15, 0)),
        ("明天 下午 3 点", datetime(2026, 11, 19, 15, 0)),
        ("明天早上九点", datetime(2026, 11, 19, 9, 0)),
        ("后天早上8点", datetime(2026, 11, 20, 8, 0)),
        ("大后天中午1点", datetime(2026, 11, 21, 13, 0)),
        ("今晚8点", datetime(2026, 11, 18, 20, 0)),
        ("明晚8点", datetime(2026, 11, 19, 20, 0)),
        # 星期
        ("周一", datetime(2026, 11, 23, 18, 0)),
        ("星期一", datetime(2026, 11, 23, 18, 0)),
        ("下个星期一", datetime(2026, 11, 23, 18, 0)),
        ("本周一", datetime(2026, 11, 16, 18, 0)),
        ("下周一", datetime(2026, 11, 23, 18, 0)),
        ("下下周三", datetime(2026, 12, 2, 18, 0)),
        ("星期五晚上8点", datetime(2026, 11, 20, 20, 0)),
        ("下周一早上九点", datetime(2026, 11, 23, 9, 0)),
        # 月日
        ("11月20日", datetime(2026, 11, 20, 18, 0)),
        ("12月1号 09:00", datetime(2026, 12, 1, 9, 0)),
        # 相对时长
        ("30 分钟后", datetime(2026, 11, 18, 15, 0)),
        ("两小时后", datetime(2026, 11, 18, 16, 30)),
        ("半小时后", datetime(2026, 11, 18, 15, 0)),
        ("3天后", datetime(2026, 11, 21, 14, 30)),
        ("一周后", datetime(2026, 11, 25, 14, 30)),
        ("一个星期后", datetime(2026, 11, 25, 14, 30)),
        # 纯时间点
        ("晚上8点", datetime(2026, 11, 18, 20, 0)),
        ("下午3点半", datetime(2026, 11, 18, 15, 30)),
        ("15:30", datetime(2026, 11, 18, 15, 30)),
        # 全角数字
        ("明天下午３点", datetime(2026, 11, 19, 15, 0)),
    ],
)
def test_parse_natural_known_forms(text: str, expected: datetime) -> None:
    assert parse_natural(text, now=NOW) == expected


def test_bare_clock_rolls_forward_when_today_is_past() -> None:
    late = datetime(2026, 11, 18, 21, 0)

    assert parse_natural("晚上8点", now=late) == datetime(2026, 11, 19, 20, 0)
    assert parse_natural("中午12点", now=NOW) == datetime(2026, 11, 19, 12, 0)


def test_same_bare_clock_in_the_future_stays_today() -> None:
    morning = datetime(2026, 11, 18, 8, 0)

    assert parse_natural("上午九点一刻", now=morning) == datetime(2026, 11, 18, 9, 15)


def test_month_day_already_passed_rolls_to_next_year() -> None:
    assert parse_natural("11月10日", now=NOW) == datetime(2027, 11, 10, 18, 0)


def test_aware_now_is_converted_to_local_naive() -> None:
    aware_now = NOW.astimezone()

    result = parse_natural("30分钟后", now=aware_now)

    assert result == NOW + timedelta(minutes=30)
    assert result.tzinfo is None


def test_default_now_is_used_when_not_injected() -> None:
    result = parse_natural("30分钟后")

    assert result.tzinfo is None
    assert result > datetime.now() - timedelta(minutes=1)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "明天下午",  # 只有时段，没有具体时间
        "随便写点什么",
        "25点",
        "3点70分",
        "2月30日",
        "2026-13-01",
        "下周八",
        "0分钟后",
        "-5分钟后",
    ],
)
def test_invalid_text_raises_value_error(text: str) -> None:
    with pytest.raises(ValueError):
        parse_natural(text, now=NOW)


@pytest.mark.parametrize("bad", [None, 123, ["明天"]])
def test_non_string_raises_value_error(bad) -> None:
    with pytest.raises(ValueError):
        parse_natural(bad, now=NOW)  # type: ignore[arg-type]


def test_bad_now_type_raises_value_error() -> None:
    with pytest.raises(ValueError):
        parse_natural("明天", now="2026-11-18")  # type: ignore[arg-type]
