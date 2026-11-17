"""工作台数据模型与时间工具。

``Task`` 是待办/日程的最小数据单元。落库时所有时间字段统一使用 epoch 秒
（int），对外通过 :func:`to_local_iso` 转成本地时间的 ISO 8601 文本。

本模块只做「格式明确」的解析（``YYYY-MM-DD HH:MM`` 等），不做自然语言
时间理解，那属于后续 Slice。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# 状态与优先级的唯一事实来源，store 校验和 CLI 展示都引用这里
STATUSES: tuple[str, ...] = ("open", "done", "cancelled")
PRIORITIES: tuple[str, ...] = ("low", "medium", "high")

# 只写日期时，默认按本地 18:00 计算截止时间
DATE_ONLY_HOUR = 18

_DATE_ONLY_FORMAT = "%Y-%m-%d"


@dataclass(frozen=True, slots=True)
class Task:
    """一条待办/日程。

    ``tags`` 用 list 保存（对外就是普通列表），``*_ts`` 字段都是 epoch 秒；
    需要人类可读时间时用 ``due_at`` / ``created_at`` 等属性，或
    :func:`to_local_iso`。
    """

    id: str
    title: str
    status: str = "open"
    priority: str = "medium"
    due_ts: int | None = None
    remind_minutes: int | None = None
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    dedupe_key: str | None = None
    created_ts: int = 0
    updated_ts: int = 0
    completed_ts: int | None = None
    reminded_ts: int | None = None

    @property
    def due_at(self) -> datetime | None:
        """截止时间的本地 ``datetime``；没有截止时间时返回 None。"""

        return to_local_datetime(self.due_ts)

    @property
    def created_at(self) -> datetime:
        return to_local_datetime(self.created_ts) or datetime.fromtimestamp(0)

    @property
    def updated_at(self) -> datetime:
        return to_local_datetime(self.updated_ts) or datetime.fromtimestamp(0)

    @property
    def completed_at(self) -> datetime | None:
        return to_local_datetime(self.completed_ts)

    @property
    def reminded_at(self) -> datetime | None:
        return to_local_datetime(self.reminded_ts)

    def to_dict(self) -> dict[str, Any]:
        """转成普通 dict，方便以后给 LLM 工具或 Web UI 直接序列化。"""

        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "priority": self.priority,
            "due_ts": self.due_ts,
            "due_at": to_local_iso(self.due_ts),
            "remind_minutes": self.remind_minutes,
            "tags": list(self.tags),
            "notes": self.notes,
            "dedupe_key": self.dedupe_key,
            "created_ts": self.created_ts,
            "updated_ts": self.updated_ts,
            "completed_ts": self.completed_ts,
            "reminded_ts": self.reminded_ts,
        }


def to_local_datetime(ts: int | None) -> datetime | None:
    """epoch 秒 -> 本地 ``datetime``；None 原样返回。"""

    if ts is None:
        return None
    return datetime.fromtimestamp(ts)


def to_local_iso(ts: int | None) -> str | None:
    """epoch 秒 -> 本地 ISO 8601 文本（``2026-11-20 09:00:00``）。"""

    moment = to_local_datetime(ts)
    if moment is None:
        return None
    return moment.isoformat(sep=" ", timespec="seconds")


def is_date_only(value: object) -> bool:
    """判断入参是不是「只有日期」的字符串，CLI 用它提示 18:00 默认值。"""

    if not isinstance(value, str):
        return False
    text = value.strip()
    if len(text) != 10:
        return False
    try:
        datetime.strptime(text, _DATE_ONLY_FORMAT)
    except ValueError:
        return False
    return True


def parse_timestamp(value: datetime | str | None, *, field: str = "时间") -> int | None:
    """把 ``datetime`` 或时间字符串解析成 epoch 秒。

    规则：
      * ``None`` -> ``None``；
      * ``datetime``：naive 视为本机时间，aware 按其时区换算；
      * 字符串：主格式 ``YYYY-MM-DD HH:MM``，另外兼容带秒、带 ``T`` 的
        ISO 形式；只写 ``YYYY-MM-DD`` 时按本地 18:00 计算；
      * 其他一切输入抛 ``ValueError``。
    """

    if value is None:
        return None
    if isinstance(value, datetime):
        return int(value.timestamp())

    if not isinstance(value, str):
        # 对外统一按「非法时间值」抛 ValueError，调用方只需要 catch 一种异常
        raise ValueError(  # noqa: TRY004
            f"{field} 只支持 datetime 或字符串，当前类型：{type(value).__name__}"
        )

    text = value.strip()
    if not text:
        raise ValueError(f"{field} 不能是空字符串")

    try:
        if is_date_only(text):
            moment = datetime.strptime(text, _DATE_ONLY_FORMAT).replace(
                hour=DATE_ONLY_HOUR
            )
        else:
            moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f'无法解析{field}：{value!r}，请使用 "YYYY-MM-DD HH:MM" 格式'
        ) from exc
    return int(moment.timestamp())
