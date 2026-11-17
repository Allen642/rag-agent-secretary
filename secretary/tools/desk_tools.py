"""工作台（待办/日程/提醒）工具集。

工具直接读写 :class:`~secretary.desk.store.DeskStore`，供 ``desk_expert``
在对话中真实落库。设计约定：

  * ``due_at`` 接受自然语言，工具内部统一交给
    :func:`~secretary.desk.timeparse.parse_natural` 解析，既支持
    ``2026-11-19 15:00`` 这类绝对格式，也支持「明天下午3点」；
  * 只暴露日常操作，``delete_task(hard=True)``、``get_task``、统计和通知
    读写留给管理 CLI，避免 LLM 误删或批量改状态；
  * 返回简洁中文文本，新增/更新结果都带任务 id，方便后续完成、改期时定位。
"""

from __future__ import annotations

import time
from datetime import datetime

from langchain_core.tools import BaseTool, tool

from ..desk import STATUS_LABELS, DeskStore, Task, parse_natural, to_local_iso

# 工具名集合，编排层和离线演示模型用它判断某个 Worker 挂载了哪些工具
DESK_TOOL_NAMES: tuple[str, ...] = (
    "add_task",
    "update_task",
    "complete_task",
    "reopen_task",
    "cancel_task",
    "list_tasks",
    "list_due",
)


def _parse_due(due_at: str | None) -> datetime | None:
    """把自然语言时间解析成本地 ``datetime``；空值表示不设置截止时间。"""

    if due_at is None:
        return None
    text = due_at.strip()
    if not text:
        return None
    return parse_natural(text)


def _format_task(task: Task) -> str:
    """把任务格式化成一行中文摘要，首字段始终是 id。"""

    status = STATUS_LABELS.get(task.status, task.status)
    due = to_local_iso(task.due_ts) or "未设置"
    text = f"{task.id} | {task.title} | {status} | 截止 {due}"
    if task.remind_minutes is not None:
        text += f" | 提前 {task.remind_minutes} 分钟提醒"
    return text


def _format_task_list(tasks: list[Task], heading: str) -> str:
    """把任务列表格式化成带序号的文本；空列表给出明确提示。"""

    if not tasks:
        return f"{heading}：暂无任务。"
    lines = [f"{heading}（共 {len(tasks)} 条）："]
    for index, task in enumerate(tasks, start=1):
        lines.append(f"{index}. {_format_task(task)}")
    return "\n".join(lines)


def make_desk_tools(store: DeskStore) -> list[BaseTool]:
    """绑定指定 ``DeskStore``，返回 7 个工作台工具。"""

    @tool("add_task", parse_docstring=True)
    def add_task(
        title: str,
        due_at: str | None = None,
        remind_minutes: int | None = None,
        priority: str = "medium",
        notes: str = "",
    ) -> str:
        """添加一条待办或日程，返回带任务 id 的结果。

        后续完成、改期、重开、取消都要用返回里的 id 定位任务。
        相同标题且相同截止时间的 open 任务会自动复用，不会重复创建。

        Args:
            title: 任务标题，例如「交房租」。
            due_at: 截止时间，支持自然语言，例如「明天下午3点」或
                「2026-11-19 15:00」；不传表示没有截止时间。
            remind_minutes: 提前多少分钟提醒，必须和 due_at 一起使用。
            priority: 优先级，只能是 low、medium、high。
            notes: 备注说明。

        Returns:
            形如「已添加任务：<id> | <标题> | 待办 | 截止 <时间>」。
        """

        task, created = store.add_task(
            title,
            due_at=_parse_due(due_at),
            remind_minutes=remind_minutes,
            priority=priority,
            notes=notes,
        )
        prefix = "已添加任务" if created else "已存在相同任务（复用）"
        return f"{prefix}：{_format_task(task)}"

    @tool("update_task", parse_docstring=True)
    def update_task(
        task_id: str,
        title: str | None = None,
        due_at: str | None = None,
        remind_minutes: int | None = None,
        priority: str | None = None,
        notes: str | None = None,
        clear_due: bool = False,
        clear_remind: bool = False,
    ) -> str:
        """修改已有任务；只传需要改的字段，未传的保持原值。

        Args:
            task_id: 任务 id，使用 add_task 或 list_tasks 返回的 id。
            title: 新的任务标题。
            due_at: 新的截止时间，支持自然语言，例如「后天上午10点」或
                「2026-11-20 09:00」。
            remind_minutes: 新的提前提醒分钟数，必须配合截止时间。
            priority: 新的优先级，只能是 low、medium、high。
            notes: 新的备注。
            clear_due: 清空截止时间，同时清掉提前提醒；不能与 due_at 或
                remind_minutes 同时传。
            clear_remind: 只清空提前提醒，保留截止时间；不能与
                remind_minutes 同时传。

        Returns:
            形如「已更新任务：<id> | <标题> | 待办 | 截止 <时间>」。
        """

        try:
            task = store.update_task(
                task_id,
                title=title,
                due_at=_parse_due(due_at),
                remind_minutes=remind_minutes,
                priority=priority,
                notes=notes,
                clear_due=clear_due,
                clear_remind=clear_remind,
            )
        except (KeyError, ValueError) as exc:
            return f"修改失败：{exc}"
        return f"已更新任务：{_format_task(task)}"

    @tool("complete_task", parse_docstring=True)
    def complete_task(task_id: str) -> str:
        """把任务标记为已完成。

        Args:
            task_id: 任务 id，使用 add_task 或 list_tasks 返回的 id。

        Returns:
            形如「已完成任务：<id> | <标题> | 已完成 | 截止 <时间>」。
        """

        try:
            task = store.complete_task(task_id)
        except (KeyError, ValueError) as exc:
            return f"完成失败：{exc}"
        return f"已完成任务：{_format_task(task)}"

    @tool("reopen_task", parse_docstring=True)
    def reopen_task(task_id: str) -> str:
        """重新打开已完成或已取消的任务，并重置提醒状态。

        Args:
            task_id: 任务 id，使用 add_task 或 list_tasks 返回的 id。

        Returns:
            形如「已重新打开任务：<id> | <标题> | 待办 | 截止 <时间>」。
        """

        try:
            task = store.reopen_task(task_id)
        except (KeyError, ValueError) as exc:
            return f"重新打开失败：{exc}"
        return f"已重新打开任务：{_format_task(task)}"

    @tool("cancel_task", parse_docstring=True)
    def cancel_task(task_id: str) -> str:
        """取消任务（软删除，记录仍保留，可用 reopen_task 恢复）。

        Args:
            task_id: 任务 id，使用 add_task 或 list_tasks 返回的 id。

        Returns:
            形如「已取消任务：<id> | <标题> | 已取消 | 截止 <时间>」。
        """

        try:
            task = store.cancel_task(task_id)
        except (KeyError, ValueError) as exc:
            return f"取消失败：{exc}"
        return f"已取消任务：{_format_task(task)}"

    @tool("list_tasks", parse_docstring=True)
    def list_tasks(
        status: str | None = None,
        include_done: bool = False,
        query: str | None = None,
        limit: int = 20,
    ) -> str:
        """按截止时间升序列出任务，默认只列进行中的任务。

        Args:
            status: 只看某个状态，只能是 open、done、cancelled；不传则按
                include_done 决定是否包含已完成，且默认排除已取消。
            include_done: 是否包含已完成任务，默认 False。
            query: 按标题或备注做字面量模糊匹配。
            limit: 最多返回多少条，默认 20。

        Returns:
            带序号的待办列表；没有任务时返回「暂无任务」。
        """

        try:
            tasks = store.list_tasks(
                status=status,
                include_done=include_done,
                query=query,
                limit=limit,
            )
        except ValueError as exc:
            return f"查询失败：{exc}"
        return _format_task_list(tasks, "任务列表")

    @tool("list_due", parse_docstring=True)
    def list_due() -> str:
        """列出当前已经到提醒时间、但还没提醒过的任务。

        Returns:
            带序号的任务列表；没有到期任务时返回「暂无任务」。
        """

        tasks = store.list_due(int(time.time()))
        return _format_task_list(tasks, "到提醒时间的任务")

    return [
        add_task,
        update_task,
        complete_task,
        reopen_task,
        cancel_task,
        list_tasks,
        list_due,
    ]
