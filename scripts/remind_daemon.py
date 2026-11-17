"""提醒 daemon：单进程轮询本地 sqlite，到点打印提醒并写入 notifications 表。

运行：
    .\\.venv\\Scripts\\python.exe scripts/remind_daemon.py
    .\\.venv\\Scripts\\python.exe scripts/remind_daemon.py --interval 5
    .\\.venv\\Scripts\\python.exe scripts/remind_daemon.py --once
    .\\.venv\\Scripts\\python.exe scripts/remind_daemon.py --db data/runtime/desk.db

默认每 30 秒检查一次，间隔也可以用环境变量 ``REMIND_INTERVAL_SECONDS`` 配置。
不引入线程、队列或外部服务：一个 ``while True`` 循环足够覆盖单机场景。

行为：
  * 启动后第一轮会做「补发」：已经过了截止时间但还没提醒的任务，合并成
    一条 ``missed`` 通知（标题为「错过的提醒（N 条）」），并逐条标记
    ``reminded_ts``；
  * 之后每轮用 :meth:`DeskStore.list_due` 找触发时间已到的任务，逐条写
    ``remind`` 通知并打印到终端；
  * 标记 ``reminded_ts`` 使用条件更新，重启或多进程同时跑也不会重复弹。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from secretary.desk import DEFAULT_DESK_DB, DeskStore, Task, to_local_iso

DEFAULT_INTERVAL_SECONDS = 30.0
INTERVAL_ENV = "REMIND_INTERVAL_SECONDS"


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="本地提醒 daemon：轮询 desk.db，到点打印提醒并写通知表",
        epilog=f"间隔默认 {DEFAULT_INTERVAL_SECONDS:g} 秒，也可用环境变量 "
        f"{INTERVAL_ENV} 配置",
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
        "--once",
        action="store_true",
        help="只跑一轮（含启动补发）就退出，方便手工验证",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)

    interval = _default_interval() if args.interval is None else args.interval
    if interval <= 0:
        print("错误：--interval 必须大于 0", file=sys.stderr)
        return 1

    try:
        store = DeskStore(args.db)
    except (sqlite3.Error, OSError) as exc:
        target = args.db or DEFAULT_DESK_DB
        print(f"错误：无法打开数据库（{target}）：{exc}", file=sys.stderr)
        return 1

    # 启动补发：只做一次，之后进入普通轮询
    try:
        _tick(store, int(time.time()), catch_up=True)
    except (sqlite3.Error, OSError) as exc:
        print(f"错误：启动补发失败：{exc}", file=sys.stderr)
        return 1

    if args.once:
        return 0

    print(
        f"提醒 daemon 已启动，每 {interval:g} 秒检查一次（Ctrl+C 退出）",
        flush=True,
    )
    while True:
        try:
            time.sleep(interval)
            _tick(store, int(time.time()))
        except KeyboardInterrupt:
            print("提醒 daemon 已停止", flush=True)
            return 0
        except (sqlite3.Error, OSError) as exc:
            # 数据库被短暂锁住等瞬时问题不应让 daemon 退出
            print(f"[错误] 本轮检查失败：{exc}", file=sys.stderr, flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
