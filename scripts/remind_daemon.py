"""提醒 daemon：单进程轮询本地 sqlite，到点打印提醒并写入 notifications 表。

运行：
    .\\.venv\\Scripts\\python.exe scripts/remind_daemon.py
    .\\.venv\\Scripts\\python.exe scripts/remind_daemon.py --interval 5
    .\\.venv\\Scripts\\python.exe scripts/remind_daemon.py --briefing 08:30
    .\\.venv\\Scripts\\python.exe scripts/remind_daemon.py --once
    .\\.venv\\Scripts\\python.exe scripts/remind_daemon.py --db data/runtime/desk.db

默认每 30 秒检查一次，间隔也可以用环境变量 ``REMIND_INTERVAL_SECONDS`` 配置；
每日简报时刻默认 08:00，可用 ``--briefing HH:MM`` 或环境变量 ``BRIEFING_TIME``
配置。不引入线程、队列或外部服务：一个 ``while True`` 循环足够覆盖单机场景。

行为：
  * 启动后第一轮会做「补发」：已经过了截止时间但还没提醒的任务，合并成
    一条 ``missed`` 通知（标题为「错过的提醒（N 条）」），并逐条标记
    ``reminded_ts``；
  * 之后每轮用 :meth:`DeskStore.list_due` 找触发时间已到的任务，逐条写
    ``remind`` 通知并打印到终端；
  * 每轮还会检查当天是否已发简报：过了 ``--briefing`` 时刻且当天没有
    ``briefing`` 通知时，生成「今日待办 + 已过期未完成」并写入通知表，
    重启后靠 ``has_briefing_since`` 去重，不会重复发；
  * 标记 ``reminded_ts`` 使用条件更新，重启或多进程同时跑也不会重复弹。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from secretary.desk import (
    DEFAULT_DESK_DB,
    DeskStore,
    Task,
    local_day_bounds,
    to_local_iso,
)

DEFAULT_INTERVAL_SECONDS = 30.0
INTERVAL_ENV = "REMIND_INTERVAL_SECONDS"
DEFAULT_BRIEFING_TIME = "08:00"
BRIEFING_TIME_ENV = "BRIEFING_TIME"


def _configure_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _print_reminder(task: Task) -> None:
    """终端提醒：标题、截止时间、任务 ID 各占一行，方便复制 ID 去处理任务。"""

    due = to_local_iso(task.due_ts) or "-"
    print(f"[提醒] {task.title}", flush=True)
    print(f"       截止：{due} | id={task.id}", flush=True)


def _missed_body(tasks: list[Task]) -> str:
    """把多条错过提醒拼成一段可读的正文。"""

    return "\n".join(
        f"- {task.title}（截止 {to_local_iso(task.due_ts) or '-'}）" for task in tasks
    )


def _tick(store: DeskStore, now_ts: int, *, catch_up: bool = False) -> list[Task]:
    """执行一轮扫描，返回本轮真正发出通知的任务。

    ``catch_up=True`` 只用在 daemon 启动后的第一轮：触发时间已到、但截止时间
    也已经过去的任务，合并成一条 ``missed`` 通知；其余任务仍按正常提醒处理。

    先 ``mark_reminded`` 再写通知：条件更新成功才写，宁可极端崩溃时漏一条，
    也不要在重启后重复弹同一条提醒。
    """

    handled: list[Task] = []
    missed: list[Task] = []

    for task in store.list_due(now_ts):
        if catch_up and task.due_ts is not None and task.due_ts <= now_ts:
            missed.append(task)
            continue

        if not store.mark_reminded(task.id, now_ts):
            continue
        store.add_notification(
            kind="remind",
            task_id=task.id,
            title=task.title,
            body=f"截止：{to_local_iso(task.due_ts) or '-'}",
            due_ts=task.due_ts,
            created_ts=now_ts,
        )
        handled.append(task)
        _print_reminder(task)

    if missed:
        # 逐条条件更新；只有真正标记成功的才计入合并通知
        marked = [task for task in missed if store.mark_reminded(task.id, now_ts)]
        if marked:
            title = f"错过的提醒（{len(marked)} 条）"
            store.add_notification(
                kind="missed",
                title=title,
                body=_missed_body(marked),
                created_ts=now_ts,
            )
            handled.extend(marked)
            print(f"[补发] {title}", flush=True)
            for task in marked:
                due = to_local_iso(task.due_ts) or "-"
                print(f"       - {task.title}（截止 {due}）", flush=True)

    return handled


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    """解析 ``HH:MM``；非法输入返回 None，由调用方决定回落还是报错。"""

    text = (value or "").strip()
    try:
        hour_text, minute_text = text.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (AttributeError, TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _briefing_ts(day_start_ts: int, briefing_time: str) -> int | None:
    """把「今天 00:00 + HH:MM」换算成 epoch 秒；格式非法返回 None。"""

    parsed = _parse_hhmm(briefing_time)
    if parsed is None:
        return None
    hour, minute = parsed
    moment = datetime.fromtimestamp(day_start_ts).replace(hour=hour, minute=minute)
    return int(moment.timestamp())


def build_briefing_text(store: DeskStore, now_ts: int) -> str:
    """拼出简报正文：「今日待办」+「已过期未完成」。

    今日待办取 ``[今天 00:00, 明天 00:00)`` 内的任务；欠账取
    :meth:`DeskStore.list_overdue`，其中今天更早已过截止的任务已经出现在
    「今日待办」里，这里按 ID 去重，避免同一件事打印两遍。
    """

    day_start, day_end = local_day_bounds(now_ts)
    today = store.list_between(day_start, day_end)
    today_ids = {task.id for task in today}
    overdue = [task for task in store.list_overdue(now_ts) if task.id not in today_ids]

    sections: list[str] = []
    if today:
        lines = ["今日待办："]
        lines.extend(
            f"- {task.title}（截止 {to_local_iso(task.due_ts) or '-'}）"
            for task in today
        )
        sections.append("\n".join(lines))
    else:
        sections.append("今日暂无安排。")

    if overdue:
        lines = ["已过期未完成："]
        lines.extend(
            f"- {task.title}（截止 {to_local_iso(task.due_ts) or '-'}）"
            for task in overdue
        )
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


def maybe_send_briefing(
    store: DeskStore, now_ts: int, briefing_time: str = DEFAULT_BRIEFING_TIME
) -> bool:
    """到点且当天还没发过简报时写入一条 ``briefing`` 通知，返回是否发出。

    去重完全依赖 :meth:`DeskStore.has_briefing_since`：当天已经有任意
    ``briefing`` 记录就不再写，因此 daemon 重启、``--once`` 连跑都不会重复。
    """

    day_start, _ = local_day_bounds(now_ts)
    target_ts = _briefing_ts(day_start, briefing_time)
    if target_ts is None or now_ts < target_ts:
        return False
    if store.has_briefing_since(day_start):
        return False

    body = build_briefing_text(store, now_ts)
    title = f"今日简报（{datetime.fromtimestamp(day_start).date().isoformat()}）"
    store.add_notification(
        kind="briefing",
        title=title,
        body=body,
        created_ts=now_ts,
    )
    print(f"[简报] {title}", flush=True)
    for line in body.splitlines():
        print(f"       {line}", flush=True)
    return True


def _default_interval() -> float:
    """读取环境变量里的间隔；非法值回落到默认 30 秒，避免 daemon 起不来。"""

    raw = os.getenv(INTERVAL_ENV, "").strip()
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS
    return value if value > 0 else DEFAULT_INTERVAL_SECONDS


def _default_briefing_time() -> str:
    """读取 ``BRIEFING_TIME``；缺失或格式非法时回落到 08:00。"""

    raw = os.getenv(BRIEFING_TIME_ENV, "").strip()
    if not raw or _parse_hhmm(raw) is None:
        return DEFAULT_BRIEFING_TIME
    return raw


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="本地提醒 daemon：轮询 desk.db，到点打印提醒并写通知表",
        epilog=f"间隔默认 {DEFAULT_INTERVAL_SECONDS:g} 秒，也可用环境变量 "
        f"{INTERVAL_ENV} 配置；简报默认 {DEFAULT_BRIEFING_TIME}，"
        f"可用 {BRIEFING_TIME_ENV} 配置",
    )
    parser.add_argument(
        "--db",
        default=None,
        help=f"sqlite 数据库路径，默认 {DEFAULT_DESK_DB}",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help=f"轮询间隔（秒），默认 {DEFAULT_INTERVAL_SECONDS:g}",
    )
    parser.add_argument(
        "--briefing",
        default=None,
        metavar="HH:MM",
        help=f"每天发送简报的时刻，默认 {DEFAULT_BRIEFING_TIME}",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只跑一轮（含启动补发和简报检查）就退出，方便手工验证",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)

    interval = _default_interval() if args.interval is None else args.interval
    if interval <= 0:
        print("错误：--interval 必须大于 0", file=sys.stderr)
        return 1

    briefing_time = (
        _default_briefing_time() if args.briefing is None else args.briefing.strip()
    )
    if _parse_hhmm(briefing_time) is None:
        print("错误：--briefing 必须是 HH:MM 格式（例如 08:00）", file=sys.stderr)
        return 1

    try:
        store = DeskStore(args.db)
    except (sqlite3.Error, OSError) as exc:
        target = args.db or DEFAULT_DESK_DB
        print(f"错误：无法打开数据库（{target}）：{exc}", file=sys.stderr)
        return 1

    # 启动补发 + 当天简报检查：只做一次，之后进入普通轮询
    try:
        now_ts = int(time.time())
        _tick(store, now_ts, catch_up=True)
        maybe_send_briefing(store, now_ts, briefing_time)
    except (sqlite3.Error, OSError) as exc:
        print(f"错误：启动检查失败：{exc}", file=sys.stderr)
        return 1

    if args.once:
        return 0

    print(
        f"提醒 daemon 已启动，每 {interval:g} 秒检查一次，"
        f"每天 {briefing_time} 发送简报（Ctrl+C 退出）",
        flush=True,
    )
    while True:
        try:
            time.sleep(interval)
            now_ts = int(time.time())
            _tick(store, now_ts)
            maybe_send_briefing(store, now_ts, briefing_time)
        except KeyboardInterrupt:
            print("提醒 daemon 已停止", flush=True)
            return 0
        except (sqlite3.Error, OSError) as exc:
            # 数据库被短暂锁住等瞬时问题不应让 daemon 退出
            print(f"[错误] 本轮检查失败：{exc}", file=sys.stderr, flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
