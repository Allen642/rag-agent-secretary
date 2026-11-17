"""本地工作台（待办/日程）管理 CLI。

用途：
  * 手动添加待办，指定截止时间和提前提醒；
  * 查看、完成、重开、取消任务；
  * 默认软删除，只有显式 ``--hard`` 才真正删行。

运行：
    python scripts/desk.py add "交房租" --due "2026-11-20 09:00" --priority high --remind 60
    python scripts/desk.py list
    python scripts/desk.py list --all
    python scripts/desk.py done <id>
    python scripts/desk.py reopen <id>
    python scripts/desk.py cancel <id>
    python scripts/desk.py rm <id>            # 软删除
    python scripts/desk.py rm <id> --hard     # 彻底删除

``--db PATH`` 可以放在子命令前后，例如
``python scripts/desk.py --db data/runtime/desk.db list`` 或
``python scripts/desk.py list --db data/runtime/desk.db``。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from secretary.desk import (
    DEFAULT_DESK_DB,
    PRIORITY_LABELS,
    STATUS_LABELS,
    DeskStore,
    Task,
    is_date_only,
    to_local_iso,
)


def _configure_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _describe(task: Task) -> str:
    """单行详情：id、状态、优先级、截止时间，以及可选的提醒/标签/备注。"""

    parts = [
        f"id={task.id}",
        f"状态={STATUS_LABELS.get(task.status, task.status)}",
        f"优先级={PRIORITY_LABELS.get(task.priority, task.priority)}",
        f"截止={to_local_iso(task.due_ts) or '-'}",
    ]
    if task.remind_minutes is not None:
        parts.append(f"提前={task.remind_minutes} 分钟")
    if task.tags:
        parts.append("标签=" + ",".join(task.tags))
    if task.notes:
        parts.append(f"备注={task.notes}")
    return " | ".join(parts)


def _split_db_arg(argv: list[str]) -> tuple[str | None, list[str]]:
    """从 argv 中取出 ``--db PATH`` / ``--db=PATH``，两种位置都支持。

    argparse 的 ``parents`` 会共享 Action 对象，子命令的默认值会覆盖父解析器
    已经解析出的值，导致 ``--db`` 写在子命令前面时被静默丢弃。这里手动提取，
    行为更可预测。
    """

    remaining: list[str] = []
    db_path: str | None = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--db":
            if index + 1 >= len(argv):
                raise ValueError("--db 后面需要一个数据库路径")
            db_path = argv[index + 1]
            index += 2
            continue
        if arg.startswith("--db="):
            db_path = arg.split("=", 1)[1]
            index += 1
            continue
        remaining.append(arg)
        index += 1
    return db_path or None, remaining


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="个人工作台（待办/日程）管理",
        epilog="--db PATH 可写在子命令前后，默认使用 data/runtime/desk.db",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="新增待办")
    add.add_argument("title", help="任务标题")
    add.add_argument(
        "--due",
        help='截止时间，例如 "2026-11-20 09:00"；只写日期时按本地 18:00 计算',
    )
    add.add_argument(
        "--priority",
        choices=["low", "medium", "high"],
        default="medium",
        help="优先级，默认 medium",
    )
    add.add_argument(
        "--remind", type=int, help="提前多少分钟提醒（必须配合 --due 使用）"
    )
    add.add_argument("--tag", action="append", default=[], help="标签，可重复指定")
    add.add_argument("--notes", default="", help="备注")

    show = sub.add_parser("list", help="列出待办")
    show.add_argument("--all", action="store_true", help="包含已完成任务")
    show.add_argument(
        "--status", choices=["open", "done", "cancelled"], help="只显示指定状态"
    )
    show.add_argument("--query", help="按标题/备注模糊搜索")
    show.add_argument("--limit", type=int, default=50, help="最多显示条数，默认 50")

    for name, help_text in (
        ("done", "标记任务完成"),
        ("reopen", "重新打开任务"),
        ("cancel", "取消任务（软删除，保留记录）"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("id", help="任务 ID")

    remove = sub.add_parser("rm", help="删除任务，默认软删除")
    remove.add_argument("id", help="任务 ID")
    remove.add_argument("--hard", action="store_true", help="彻底删除，不可恢复")
    return parser


def _handle(store: DeskStore, args: argparse.Namespace) -> int:
    if args.command == "add":
        task, created = store.add_task(
            args.title,
            due_at=args.due,
            remind_minutes=args.remind,
            priority=args.priority,
            tags=args.tag,
            notes=args.notes,
        )
        if created:
            print(f"已添加：{task.id}")
        else:
            print(f"已存在，复用：{task.id}（如需改提醒/优先级请用 update）")
        print(f"  {_describe(task)}")
        if is_date_only(args.due):
            print("  提示：只提供了日期，已按本地 18:00 计算截止时间。")
        return 0

    if args.command == "list":
        tasks = store.list_tasks(
            status=args.status,
            include_done=args.all,
            query=args.query,
            limit=args.limit,
        )
        if not tasks:
            print("没有符合条件的任务。")
            return 0
        for index, task in enumerate(tasks, start=1):
            print(f"{index}. {task.title}")
            print(f"   {_describe(task)}")
        print(f"共 {len(tasks)} 条")
        return 0

    if args.command == "done":
        task = store.complete_task(args.id)
        print(f"已完成：{task.id} | {task.title}")
        return 0

    if args.command == "reopen":
        task = store.reopen_task(args.id)
        print(f"已重新打开：{task.id} | {task.title}")
        return 0

    if args.command == "cancel":
        task = store.cancel_task(args.id)
        print(f"已取消：{task.id} | {task.title}")
        return 0

    if args.command == "rm":
        task = store.get_task(args.id)
        if task is None:
            raise KeyError(f"任务不存在：{args.id}")
        store.delete_task(task.id, hard=args.hard)
        if args.hard:
            print(f"已彻底删除：{task.id} | {task.title}")
        else:
            print(f"已软删除（状态改为已取消）：{task.id} | {task.title}")
            print(f"如需彻底删除：python scripts/desk.py rm {task.id} --hard")
        return 0

    return 1


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        db_path, rest = _split_db_arg(raw_argv)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    args = build_parser().parse_args(rest)

    try:
        store = DeskStore(db_path)
    except (sqlite3.Error, OSError) as exc:  # 文件损坏/权限不足等，给中文提示而不是 traceback
        print(f"错误：无法打开数据库（{db_path or DEFAULT_DESK_DB}）：{exc}", file=sys.stderr)
        return 1

    try:
        return _handle(store, args)
    except KeyError as exc:
        print(f"错误：{exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
