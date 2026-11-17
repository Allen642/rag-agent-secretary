"""基于标准库 sqlite3 的本地工作台存储。

数据库默认放在 ``data/runtime/desk.db``，目录不存在时自动创建。启用 WAL，
每次操作独立连接并在结束时关闭；写操作显式开启 ``BEGIN IMMEDIATE`` 事务，
避免隐式事务嵌套，也方便以后把同一套逻辑复用到提醒进程。

设计约定：
  * 主键使用 ``uuid4().hex``，不用内容哈希，改名不会产生新任务；
  * 时间统一存 epoch 秒，展示交给 :func:`secretary.desk.models.to_local_iso`；
  * 软删除 = ``status='cancelled'``，默认不出现在列表里；
  * ``remind_minutes`` 必须配合 ``due_ts`` 使用。
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..config import DATA_DIR, PROJECT_ROOT
from .models import PRIORITIES, STATUSES, Task, parse_timestamp

# 相对路径一律基于项目根目录解析，避免受启动目录影响
DEFAULT_DESK_DB = DATA_DIR / "runtime" / "desk.db"

_COLUMNS = (
    "id, title, status, priority, due_ts, remind_minutes, tags, notes, "
    "dedupe_key, created_ts, updated_ts, completed_ts, reminded_ts"
)

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id             TEXT PRIMARY KEY,
        title          TEXT NOT NULL,
        status         TEXT NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open', 'done', 'cancelled')),
        priority       TEXT NOT NULL DEFAULT 'medium'
                       CHECK (priority IN ('low', 'medium', 'high')),
        due_ts         INTEGER,
        remind_minutes INTEGER,
        tags           TEXT NOT NULL DEFAULT '[]',
        notes          TEXT NOT NULL DEFAULT '',
        dedupe_key     TEXT,
        created_ts     INTEGER NOT NULL,
        updated_ts     INTEGER NOT NULL,
        completed_ts   INTEGER,
        reminded_ts    INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_status_due ON tasks(status, due_ts)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_dedupe ON tasks(dedupe_key, status)",
)


def _resolve_db_path(db_path: Path | str | None) -> Path:
    """把用户给的库路径解析成绝对路径；相对路径基于项目根目录。"""

    if db_path is None or str(db_path).strip() == "":
        return DEFAULT_DESK_DB
    path = Path(db_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _encode_tags(tags: Sequence[str]) -> str:
    return json.dumps(list(tags), ensure_ascii=False)


def _decode_tags(raw: Any) -> list[str]:
    """容错解析 tags 列；数据损坏时退化成空列表，避免整个列表接口挂掉。"""

    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _normalize_title(title: str) -> str:
    if not isinstance(title, str):
        raise ValueError("title 必须是字符串")  # noqa: TRY004
    cleaned = title.strip()
    if not cleaned:
        raise ValueError("标题不能为空")
    return cleaned


def _normalize_priority(priority: str) -> str:
    if priority not in PRIORITIES:
        raise ValueError(
            f"priority 必须是 {'/'.join(PRIORITIES)} 之一，当前：{priority!r}"
        )
    return priority


def _normalize_remind(remind_minutes: int | None, *, due_ts: int | None) -> int | None:
    """校验提前提醒分钟数：必须是非负整数，且必须配合截止时间。"""

    if remind_minutes is None:
        return None
    if isinstance(remind_minutes, bool) or not isinstance(remind_minutes, int):
        raise ValueError("remind_minutes 必须是整数")  # noqa: TRY004
    if remind_minutes < 0:
        raise ValueError("remind_minutes 不能为负数")
    if due_ts is None:
        raise ValueError("remind_minutes 必须配合 due_at 使用")
    return remind_minutes


def _normalize_tags(tags: Sequence[str] | None) -> list[str]:
    """去空白、去重并保持顺序；传字符串时明确报错，避免被逐字符拆开。"""

    if tags is None:
        return []
    if isinstance(tags, str):
        raise ValueError("tags 需要字符串序列，不能直接传单个字符串")  # noqa: TRY004
    cleaned: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            raise ValueError("tags 中的每个标签都必须是字符串")  # noqa: TRY004
        text = tag.strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        priority=row["priority"],
        due_ts=row["due_ts"],
        remind_minutes=row["remind_minutes"],
        tags=_decode_tags(row["tags"]),
        notes=row["notes"],
        dedupe_key=row["dedupe_key"],
        created_ts=row["created_ts"],
        updated_ts=row["updated_ts"],
        completed_ts=row["completed_ts"],
        reminded_ts=row["reminded_ts"],
    )


class DeskStore:
    """待办/日程的 sqlite3 存储。

    使用方式::

        store = DeskStore()                       # data/runtime/desk.db
        task, created = store.add_task("交房租", due_at="2026-11-20 09:00")
        store.complete_task(task.id)
        store.list_tasks()
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = _resolve_db_path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------ 连接

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """每次操作独立连接，结束即关闭；isolation_level=None 表示手动管事务。"""

        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """写事务：显式 BEGIN IMMEDIATE，异常时回滚。"""

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _init_schema(self) -> None:
        """建表/建索引；幂等，多次构造不会丢数据。"""

        with self._write() as conn:
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)
            # 给未来迁移留出版本位
            conn.execute("PRAGMA user_version = 1")

    # ------------------------------------------------------------------ 读取

    def get_task(self, task_id: str) -> Task | None:
        """按 ID 取任务，任何状态都返回（含已取消）；不存在返回 None。"""

        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return _row_to_task(row) if row is not None else None

    def list_tasks(
        self,
        *,
        status: str | None = None,
        include_done: bool = False,
        query: str | None = None,
        limit: int = 50,
    ) -> list[Task]:
        """按截止时间升序列出任务（无截止时间的排最后）。

        默认排除已取消（软删除）和已完成；``include_done=True`` 带上已完成；
        显式传 ``status`` 时以它为准（``status="cancelled"`` 可看软删除记录）。
        ``query`` 对标题和备注做字面量模糊匹配（``%``/``_`` 不当通配符）。
        """

        if status is not None and status not in STATUSES:
            raise ValueError(f"status 必须是 {'/'.join(STATUSES)} 之一，当前：{status!r}")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit 必须是大于 0 的整数")
        if query is not None and not isinstance(query, str):
            raise ValueError("query 必须是字符串")

        clauses: list[str] = []
        params: list[Any] = []

        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        else:
            clauses.append("status <> 'cancelled'")
            if not include_done:
                clauses.append("status <> 'done'")

        keyword = (query or "").strip()
        if keyword:
            pattern = f"%{_escape_like(keyword)}%"
            clauses.append(r"(title LIKE ? ESCAPE '\' OR notes LIKE ? ESCAPE '\')")
            params.extend([pattern, pattern])

        sql = (
            f"SELECT {_COLUMNS} FROM tasks WHERE {' AND '.join(clauses)} "
            "ORDER BY due_ts IS NULL, due_ts ASC, created_ts ASC, id ASC "
            "LIMIT ?"
        )
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_task(row) for row in rows]

    def count(self, *, status: str | None = None) -> int:
        """统计任务数；``status=None`` 统计全部行（含已完成和已取消）。"""

        if status is not None and status not in STATUSES:
            raise ValueError(f"status 必须是 {'/'.join(STATUSES)} 之一，当前：{status!r}")

        with self._connect() as conn:
            if status is None:
                row = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status = ?", (status,)
                ).fetchone()
        return int(row[0])

    # ------------------------------------------------------------------ 写入

    def add_task(
        self,
        title: str,
        *,
        due_at: Any = None,
        remind_minutes: int | None = None,
        priority: str = "medium",
        tags: Sequence[str] = (),
        notes: str = "",
        dedupe_key: str | None = None,
    ) -> tuple[Task, bool]:
        """新增任务，返回 ``(task, created)``。

        ``created`` 为 ``True`` 表示真的插入了新行；为 ``False`` 表示去重命中，
        直接复用已有任务——此时**不**刷新 ``updated_ts``，也**不**用本次传入的
        ``priority`` / ``tags`` / ``remind_minutes`` / ``notes`` 覆盖已有记录。

        去重规则：``dedupe_key`` 非空时只按它匹配 ``status='open'`` 的任务；
        否则按「同标题 + 同 due_ts + status='open'」匹配。
        """

        cleaned_title = _normalize_title(title)
        cleaned_priority = _normalize_priority(priority)
        due_ts = parse_timestamp(due_at, field="due_at")
        remind = _normalize_remind(remind_minutes, due_ts=due_ts)
        tag_list = _normalize_tags(tags)
        cleaned_notes = "" if notes is None else str(notes).strip()
        key = (str(dedupe_key).strip() or None) if dedupe_key is not None else None

        now = int(time.time())
        with self._write() as conn:
            existing = self._find_duplicate(
                conn, title=cleaned_title, due_ts=due_ts, dedupe_key=key
            )
            if existing is not None:
                return existing, False

            task = Task(
                id=uuid4().hex,
                title=cleaned_title,
                status="open",
                priority=cleaned_priority,
                due_ts=due_ts,
                remind_minutes=remind,
                tags=tag_list,
                notes=cleaned_notes,
                dedupe_key=key,
                created_ts=now,
                updated_ts=now,
            )
            conn.execute(
                f"INSERT INTO tasks ({_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task.id,
                    task.title,
                    task.status,
                    task.priority,
                    task.due_ts,
                    task.remind_minutes,
                    _encode_tags(task.tags),
                    task.notes,
                    task.dedupe_key,
                    task.created_ts,
                    task.updated_ts,
                    task.completed_ts,
                    task.reminded_ts,
                ),
            )
        return task, True

    def complete_task(self, task_id: str) -> Task:
        """标记完成；已完成时幂等返回，已取消的任务不允许完成。"""

        with self._write() as conn:
            row = self._require_row(conn, task_id)
            if row["status"] == "cancelled":
                raise ValueError("已取消的任务不能标记完成，请先 reopen")
            if row["status"] == "done":
                return _row_to_task(row)

            now = int(time.time())
            conn.execute(
                "UPDATE tasks SET status = 'done', completed_ts = ?, updated_ts = ? "
                "WHERE id = ?",
                (now, now, task_id),
            )
            return _row_to_task(self._require_row(conn, task_id))

    def reopen_task(self, task_id: str) -> Task:
        """重新打开任务；清空 completed_ts 和 reminded_ts，让提醒可以再次触发。"""

        with self._write() as conn:
            row = self._require_row(conn, task_id)
            if row["status"] == "open":
                return _row_to_task(row)

            now = int(time.time())
            conn.execute(
                "UPDATE tasks SET status = 'open', completed_ts = NULL, "
                "reminded_ts = NULL, updated_ts = ? WHERE id = ?",
                (now, task_id),
            )
            return _row_to_task(self._require_row(conn, task_id))

    def cancel_task(self, task_id: str) -> Task:
        """软删除：状态置为 cancelled，行仍保留；重复取消幂等。"""

        with self._write() as conn:
            row = self._require_row(conn, task_id)
            if row["status"] == "cancelled":
                return _row_to_task(row)

            now = int(time.time())
            conn.execute(
                "UPDATE tasks SET status = 'cancelled', updated_ts = ? WHERE id = ?",
                (now, task_id),
            )
            return _row_to_task(self._require_row(conn, task_id))

    def update_task(
        self,
        task_id: str,
        *,
        title: str | None = None,
        due_at: Any = None,
        remind_minutes: int | None = None,
        priority: str | None = None,
        tags: Sequence[str] | None = None,
        notes: str | None = None,
        clear_due: bool = False,
        clear_remind: bool = False,
    ) -> Task:
        """局部更新；参数为 None 表示「保持不变」。

        需要清空截止时间时传 ``clear_due=True``（同时清掉提前提醒和
        reminded_ts）；只清提醒用 ``clear_remind=True``。改动 due_at 或
        remind_minutes 会重置 ``reminded_ts``，让提醒按新时间重新计算。
        """

        if clear_due and due_at is not None:
            raise ValueError("due_at 与 clear_due 不能同时使用")
        if clear_due and remind_minutes is not None:
            raise ValueError("clear_due 与 remind_minutes 不能同时使用")
        if clear_remind and remind_minutes is not None:
            raise ValueError("clear_remind 与 remind_minutes 不能同时使用")

        with self._write() as conn:
            row = self._require_row(conn, task_id)
            current = _row_to_task(row)

            new_title = current.title if title is None else _normalize_title(title)
            new_priority = (
                current.priority if priority is None else _normalize_priority(priority)
            )
            new_tags = current.tags if tags is None else _normalize_tags(tags)
            new_notes = current.notes if notes is None else str(notes).strip()

            new_due = current.due_ts
            new_remind = current.remind_minutes
            new_reminded = current.reminded_ts

            if clear_due:
                new_due = None
                new_remind = None
                new_reminded = None
            elif due_at is not None:
                new_due = parse_timestamp(due_at, field="due_at")
                new_reminded = None

            if clear_remind:
                new_remind = None
                new_reminded = None
            elif remind_minutes is not None:
                new_remind = _normalize_remind(remind_minutes, due_ts=new_due)
                new_reminded = None

            if new_remind is not None and new_due is None:
                raise ValueError("remind_minutes 必须配合 due_at 使用")

            now = int(time.time())
            conn.execute(
                "UPDATE tasks SET title = ?, priority = ?, due_ts = ?, "
                "remind_minutes = ?, tags = ?, notes = ?, updated_ts = ?, "
                "reminded_ts = ? WHERE id = ?",
                (
                    new_title,
                    new_priority,
                    new_due,
                    new_remind,
                    _encode_tags(new_tags),
                    new_notes,
                    now,
                    new_reminded,
                    task_id,
                ),
            )
            return _row_to_task(self._require_row(conn, task_id))

    def delete_task(self, task_id: str, *, hard: bool = False) -> None:
        """删除任务。默认软删除（status 置 cancelled）；``hard=True`` 才真正删行。

        ``hard`` 只给管理 CLI 用，未来 LLM 工具不应暴露硬删除。
        """

        if not hard:
            self.cancel_task(task_id)
            return

        with self._write() as conn:
            cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            if cursor.rowcount == 0:
                raise KeyError(f"任务不存在：{task_id}")

    # ------------------------------------------------------------------ 内部

    def _find_duplicate(
        self,
        conn: sqlite3.Connection,
        *,
        title: str,
        due_ts: int | None,
        dedupe_key: str | None,
    ) -> Task | None:
        """在同一写事务里查找可复用的 open 任务，保证去重是原子的。"""

        if dedupe_key is not None:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM tasks "
                "WHERE dedupe_key = ? AND status = 'open' "
                "ORDER BY created_ts ASC, id ASC LIMIT 1",
                (dedupe_key,),
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM tasks "
                "WHERE title = ? AND due_ts IS ? AND status = 'open' "
                "ORDER BY created_ts ASC, id ASC LIMIT 1",
                (title, due_ts),
            ).fetchone()
        return _row_to_task(row) if row is not None else None

    @staticmethod
    def _require_row(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"任务不存在：{task_id}")
        return row


def _escape_like(keyword: str) -> str:
    r"""按 ``ESCAPE '\'`` 规则转义，让 ``%``/``_`` 按字面量匹配。"""

    return keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
