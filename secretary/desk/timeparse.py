"""自然语言时间解析。

对外只有一个入口 :func:`parse_natural`：先把「格式明确」的文本交给
:func:`secretary.desk.models.parse_timestamp`，失败后再按中文习惯理解相对时间。

约定：
  * 返回值一律是**本地时区的 naive datetime**；
  * 只写日期（包括「明天」「下周一」这类只给出日期的说法）时，沿用项目已有
    的 18:00 默认值（``models.DATE_ONLY_HOUR``）；
  * 只写时间点、没写日期时，如果该时间今天已经过去，就顺延到明天；
  * 裸数字时间点（``3点``、``3点半``）按 24 小时制字面理解，口语请带
    「上午/下午/晚上」，否则可能落到凌晨；
  * 只写星期几（如 ``周五``）时按最近一次计算，包含当天；
  * 无法识别、月份日期越界、小时/分钟越界时直接抛 ``ValueError``，不做
    模糊兜底，也不返回「现在」之类的近似值。

支持的写法示例：
  * 绝对时间：``2026-11-20 09:00``、``2026-11-20``、``2026-11-20T09:00:00``；
  * 日期词：``今天``、``明天``、``后天``、``大后天``、``昨天``、``前天``；
  * 星期：``周一``、``星期一``、``本周一``、``下周一``、``下下周三``；
  * 月日：``11月20日``、``12月1号``；
  * 相对时长：``30 分钟后``、``两小时后``、``半小时后``、``3天后``；
  * 时间点：``下午3点``、``晚上8点``、``15:30``、``下午3点半``、``上午九点一刻``。
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from .models import DATE_ONLY_HOUR, parse_timestamp

# 全角数字/冒号/空格统一成半角，降低中文输入法的干扰
_TRANSLATION = str.maketrans(
    {
        "０": "0",
        "１": "1",
        "２": "2",
        "３": "3",
        "４": "4",
        "５": "5",
        "６": "6",
        "７": "7",
        "８": "8",
        "９": "9",
        "：": ":",
        "　": " ",
    }
)

_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

_DAY_WORD_OFFSETS = {
    "今天": 0,
    "今日": 0,
    "明天": 1,
    "明日": 1,
    "后天": 2,
    "大后天": 3,
    "昨天": -1,
    "前天": -2,
}

_DAY_WORD_RE = re.compile(
    r"(?P<word>今天|今日|明天|明日|后天|大后天|昨天|前天)(?P<rest>.*)"
)

_WEEKDAY_RE = re.compile(
    r"(?P<mod>下下|下|本|这|上)?(?:个)?(?:周|星期|礼拜)"
    r"(?P<weekday>[一二三四五六日天])(?P<rest>.*)"
)

# 「今晚」「明早」这类口语先展开成完整的日期词 + 时段。
# 裸「今晚/明晚」等若不跟具体时间（如 ``明晚`` 而非 ``明晚8点``）会抛
# ValueError，因为时段词本身不含时间点。
_DAY_ALIASES = {
    "今晚": "今天晚上",
    "明晚": "明天晚上",
    "昨晚": "昨天晚上",
    "今早": "今天早上",
    "明早": "明天早上",
}

_MONTH_DAY_RE = re.compile(r"(?P<month>\d{1,2})月(?P<day>\d{1,2})[日号](?P<rest>.*)")

_DURATION_UNITS = {
    "秒钟": 1,
    "分钟": 60,
    "小时": 3600,
    "个小时": 3600,
    "钟头": 3600,
    "个星期": 604800,
    "个礼拜": 604800,
    "星期": 604800,
    "礼拜": 604800,
    "秒": 1,
    "分": 60,
    "天": 86400,
    "日": 86400,
    "周": 604800,
}

_DURATION_RE = re.compile(
    r"(?P<num>\d+|半|[零〇一二两三四五六七八九十]+)"
    r"(?P<unit>秒钟|分钟|小时|个小时|钟头|个星期|个礼拜|星期|礼拜|秒|分|天|日|周)"
    r"(?:之后|以后|后)"
)

_PERIODS = ("凌晨", "早上", "早晨", "上午", "中午", "下午", "傍晚", "晚上", "夜里")
_PM_PERIODS = frozenset({"下午", "傍晚", "晚上", "夜里"})

_PERIOD_RE = re.compile(
    r"(?:(?P<period>" + "|".join(_PERIODS) + r"))?(?P<clock>.+)"
)

_CLOCK_COLON_RE = re.compile(r"(?P<hour>\d{1,2}):(?P<minute>\d{1,2})")

_CLOCK_DIAN_RE = re.compile(
    r"(?P<hour>\d{1,2}|[零〇一二两三四五六七八九十]{1,3})(?:点|时)"
    r"(?:(?P<half>半)|(?P<quarter>一刻|三刻)|"
    r"(?P<minute>\d{1,2}|[零〇一二两三四五六七八九十]{1,3})分?)?"
)


def parse_natural(text: str, *, now: datetime | None = None) -> datetime:
    """把自然语言时间解析成本地 naive ``datetime``。

    ``now`` 用于注入「当前时间」，方便测试；不传时使用 :func:`datetime.now`。
    解析失败一律抛 ``ValueError``，不会静默返回当前时间或近似值。
    """

    if not isinstance(text, str):
        raise ValueError(  # noqa: TRY004
            f"时间只支持字符串，当前类型：{type(text).__name__}"
        )
    raw = text.strip()
    if not raw:
        raise ValueError("时间不能是空字符串")

    # 1) 绝对时间优先走既有解析器，保证与 store/CLI 的行为完全一致
    try:
        abs_ts = parse_timestamp(raw, field="时间")
    except ValueError:
        pass
    else:
        assert abs_ts is not None  # raw 非空，parse_timestamp 不会返回 None
        return datetime.fromtimestamp(abs_ts)

    # 2) 相对时间：先去掉空白，避免「30 分钟后」「明天 下午 3 点」被空格干扰
    normalized = "".join(raw.translate(_TRANSLATION).split())
    normalized = _expand_aliases(normalized)
    base = _naive_local(now) if now is not None else datetime.now()

    result = _parse_relative(normalized, base)
    if result is None:
        raise ValueError(
            f"无法解析时间：{text!r}，"
            '请使用 "YYYY-MM-DD HH:MM" 或「明天下午3点」「30分钟后」这类写法'
        )
    return result


def _expand_aliases(text: str) -> str:
    """把「今晚 / 明早」展开成「今天晚上 / 明天早上」；只处理开头。"""

    for alias, expanded in _DAY_ALIASES.items():
        if text.startswith(alias):
            return expanded + text[len(alias) :]
    return text


def _naive_local(moment: datetime) -> datetime:
    """带时区的 ``datetime`` 先换算到本机时区，再去掉 tzinfo；naive 原样返回。"""

    if not isinstance(moment, datetime):
        raise ValueError(  # noqa: TRY004
            f"now 必须是 datetime，当前类型：{type(moment).__name__}"
        )
    if moment.tzinfo is None:
        return moment
    return moment.astimezone().replace(tzinfo=None)


def _parse_relative(text: str, base: datetime) -> datetime | None:
    """依次尝试相对时长、日期词、星期、月日、纯时间点；都不匹配返回 None。"""

    duration = _parse_duration(text)
    if duration is not None:
        return base + duration

    day_word = _DAY_WORD_RE.fullmatch(text)
    if day_word is not None:
        offset = _DAY_WORD_OFFSETS[day_word.group("word")]
        target = base.date() + timedelta(days=offset)
        return _combine_with_clock(target, day_word.group("rest"))

    weekday = _WEEKDAY_RE.fullmatch(text)
    if weekday is not None:
        target = _resolve_weekday(
            weekday.group("mod"), weekday.group("weekday"), base.date()
        )
        return _combine_with_clock(target, weekday.group("rest"))

    month_day = _MONTH_DAY_RE.fullmatch(text)
    if month_day is not None:
        target = _resolve_month_day(
            int(month_day.group("month")), int(month_day.group("day")), base.date()
        )
        return _combine_with_clock(target, month_day.group("rest"))

    clock = _parse_clock(text)
    if clock is None:
        return None
    hour, minute = clock
    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # 只给时间点时，今天已经过点就顺延到明天
    if candidate <= base:
        candidate += timedelta(days=1)
    return candidate


def _parse_duration(text: str) -> timedelta | None:
    """解析「N 分钟后」这类纯相对时长；不是该形式时返回 None。"""

    match = _DURATION_RE.fullmatch(text)
    if match is None:
        return None

    token = match.group("num")
    if token == "半":
        amount = 0.5
    else:
        amount = _to_int(token, field="时长")
    seconds = amount * _DURATION_UNITS[match.group("unit")]
    if seconds <= 0:
        raise ValueError("相对时长必须大于 0")
    return timedelta(seconds=seconds)


def _resolve_weekday(mod: str | None, weekday: str, today: date) -> date:
    """把「周一 / 下周一 / 本周一」换算成具体日期。"""

    index = _WEEKDAY_INDEX[weekday]
    if mod is None:
        # 裸星期：取最近一次（含今天）
        return today + timedelta(days=(index - today.weekday()) % 7)

    monday = today - timedelta(days=today.weekday())
    if mod == "下":
        monday += timedelta(days=7)
    elif mod == "下下":
        monday += timedelta(days=14)
    elif mod == "上":
        monday -= timedelta(days=7)
    # 「本 / 这」保持本周
    return monday + timedelta(days=index)


def _resolve_month_day(month: int, day: int, today: date) -> date:
    """解析「X月Y日」；今年的日期已经过去时顺延到明年。"""

    for year in (today.year, today.year + 1):
        try:
            candidate = datetime(year, month, day).date()
        except ValueError:
            continue
        if candidate >= today:
            return candidate
    raise ValueError(f"无效的日期：{month}月{day}日")


def _combine_with_clock(target: date, rest: str) -> datetime:
    """把日期和可选时间拼起来；只有日期时按 18:00 计算。"""

    expr = rest.strip()
    if not expr:
        return datetime.combine(target, time(hour=DATE_ONLY_HOUR, minute=0))

    clock = _parse_clock(expr)
    if clock is None:
        raise ValueError(f"日期后面的时间无法识别：{expr!r}")
    hour, minute = clock
    try:
        return datetime.combine(target, time(hour=hour, minute=minute))
    except ValueError as exc:
        raise ValueError(f"无效的时间：{hour:02d}:{minute:02d}") from exc


def _parse_clock(expr: str) -> tuple[int, int] | None:
    """解析时间段（可选上午/下午）成 24 小时制 ``(hour, minute)``。

    不是时间点形式时返回 None；形式匹配但数值越界时抛 ``ValueError``。
    """

    text = expr.strip()
    if not text:
        return None

    match = _PERIOD_RE.fullmatch(text)
    if match is None:
        return None
    period = match.group("period")
    body = match.group("clock")

    colon = _CLOCK_COLON_RE.fullmatch(body)
    if colon is not None:
        hour = int(colon.group("hour"))
        minute = int(colon.group("minute"))
    else:
        dian = _CLOCK_DIAN_RE.fullmatch(body)
        if dian is None:
            return None
        hour = _to_int(dian.group("hour"), field="小时")
        if dian.group("half"):
            minute = 30
        elif dian.group("quarter"):
            minute = 15 if dian.group("quarter") == "一刻" else 45
        elif dian.group("minute"):
            minute = _to_int(dian.group("minute"), field="分钟")
        else:
            minute = 0

    if not 0 <= hour <= 23:
        raise ValueError(f"小时超出范围：{hour}")
    if not 0 <= minute <= 59:
        raise ValueError(f"分钟超出范围：{minute}")

    if period in _PM_PERIODS:
        if 0 < hour < 12:
            hour += 12
    elif period == "中午":
        if 1 <= hour < 11:
            hour += 12
    elif period == "凌晨" and hour == 12:
        hour = 0

    return hour, minute


def _to_int(token: str, *, field: str) -> int:
    """把阿拉伯数字或简单中文数字（含「十」组合）转成 int。"""

    if token.isdigit():
        return int(token)

    if "十" in token:
        left, _, right = token.partition("十")
        if len(left) > 1 or len(right) > 1:
            raise ValueError(f"{field}无法识别：{token!r}")
        tens = _CN_DIGITS[left] if left else 1
        ones = _CN_DIGITS[right] if right else 0
        return tens * 10 + ones

    if len(token) == 1 and token in _CN_DIGITS:
        return _CN_DIGITS[token]

    raise ValueError(f"{field}无法识别：{token!r}")
