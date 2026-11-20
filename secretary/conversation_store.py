"""企业微信会话历史的进程内 / SQLite 持久化存储。

``ConversationStore`` 保持原有公开接口（``lock_for`` / ``get`` / ``set``），
调用方不需要关心底层是内存还是 SQLite：

* ``db_path is None``：沿用内存实现。``OrderedDict`` 保存每个用户最近
  ``max_messages`` 条消息，超过 ``max_users`` 个用户时按 LRU 淘汰；
  ``get`` 也会把用户移到队尾，视为一次使用。
* ``db_path`` 非空：历史写入 SQLite。表结构为::

      CREATE TABLE conversations (
          user_id    TEXT PRIMARY KEY,
          history    TEXT NOT NULL,
          updated_at INTEGER NOT NULL
      );

  ``history`` 是 ``langchain_core.messages.messages_to_dict`` 的 JSON 结果，
  因此 ``HumanMessage`` / ``AIMessage``（包括 ``AIMessage.name``）都能原样
  恢复。``updated_at`` 是毫秒时间戳，写入和读取都会刷新它，用它实现与内存
  模式一致的 LRU：只保留最新 ``max_users`` 个用户；``max_messages`` 的裁剪
  在写入前完成。每次操作使用独立连接（``timeout=5.0``），并开启 WAL 与
  ``busy_timeout``，避免旧笔记本上偶发的磁盘锁竞争。

降级策略：建库或读写时出现 ``sqlite3.Error``（建目录时还可能是 ``OSError``）
只记一条 warning，然后切换到内存模式继续服务，不向调用方抛异常。企业微信
回复链路不能因为磁盘或权限问题整体失败。

用户锁 ``lock_for`` 与存储介质无关，锁对象永不淘汰，保证同一用户的消息
始终串行处理。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING

from langchain_core.messages import messages_from_dict, messages_to_dict

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage

logger = logging.getLogger("conversation_store")

# 默认每个用户保留 20 条消息，进程/数据库最多保留 256 个用户
DEFAULT_MAX_MESSAGES = 20
DEFAULT_MAX_USERS = 256

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS conversations (
    user_id    TEXT PRIMARY KEY,
    history    TEXT NOT NULL,
    updated_at INTEGER NOT NULL
)
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_conversations_updated_at
    ON conversations(updated_at)
"""

_UPSERT_HISTORY = """
INSERT INTO conversations (user_id, history, updated_at)
VALUES (?, ?, ?)
ON CONFLICT(user_id) DO UPDATE SET
    history = excluded.history,
    updated_at = excluded.updated_at
"""

# 只保留 updated_at 最新的 max_users 个用户；rowid 仅用于同毫秒写入时定序
_EVICT_USERS = """
DELETE FROM conversations
WHERE user_id NOT IN (
    SELECT user_id
    FROM conversations
    ORDER BY updated_at DESC, rowid DESC
    LIMIT ?
)
"""


class ConversationStore:
    """按用户隔离的短期对话存储，并保证同一用户串行处理。

    * 每个用户最多保留 ``max_messages`` 条消息（按用户裁剪）；
    * 整个进程 / 数据库最多保留 ``max_users`` 个用户，超出按 LRU 淘汰；
    * :meth:`lock_for` 返回该用户专属的锁，避免同一个人连发消息时
      两轮上下文互相覆盖或回复顺序错乱。

    传入 ``db_path`` 时历史落 SQLite，重启后仍可恢复；不传时退化为纯内存
    实现，行为与迁移前完全一致。
    """

    def __init__(
        self,
        *,
        db_path: Path | str | None = None,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        max_users: int = DEFAULT_MAX_USERS,
    ) -> None:
        self._max_messages = max(1, max_messages)
        self._max_users = max(1, max_users)
        self._history: OrderedDict[str, list[BaseMessage]] = OrderedDict()
        self._locks: dict[str, threading.Lock] = {}
        # 保护 _history/_locks/SQLite 写入；用户级互斥由 lock_for 返回的锁负责
        self._guard = threading.Lock()
        # 同一进程内保证 updated_at 严格递增，避免同毫秒写入无法区分先后
        self._clock = 0
        self._db_path: Path | None = None
        if db_path is not None:
            self._db_path = self._init_db(Path(db_path).expanduser())

    def lock_for(self, user_id: str) -> threading.Lock:
        """返回某个用户的专属锁；同一用户的消息会按到达顺序串行处理。"""

        with self._guard:
            lock = self._locks.get(user_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[user_id] = lock
            return lock

    def get(self, user_id: str) -> list[BaseMessage]:
        """读取该用户的历史副本；访问也会刷新 LRU 顺序。"""

        with self._guard:
            path = self._db_path
            if path is not None:
                try:
                    return self._db_get(path, user_id)
                except (sqlite3.Error, OSError) as exc:
                    self._degrade(exc)
            return self._memory_get(user_id)

    def set(self, user_id: str, history: list[BaseMessage]) -> None:
        """覆盖写入该用户的历史，并裁剪到上限、淘汰最久未使用的用户。"""

        with self._guard:
            trimmed = list(history)[-self._max_messages :]
            path = self._db_path
            if path is not None:
                try:
                    self._db_set(path, user_id, trimmed)
                    return
                except (sqlite3.Error, OSError) as exc:
                    self._degrade(exc)
            self._memory_set(user_id, trimmed)

    # ---- 内部：SQLite 模式 ----

    def _init_db(self, path: Path) -> Path | None:
        """建目录和表；失败时记 warning 并返回 ``None``（内存模式）。"""

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = self._connect(path)
            try:
                conn.execute(_CREATE_TABLE)
                conn.execute(_CREATE_INDEX)
                # 从已有数据续上时钟，避免系统时间回拨或同毫秒重启后
                # 新写入的 updated_at 小于旧行，导致刚写的用户被立刻淘汰
                row = conn.execute(
                    "SELECT COALESCE(MAX(updated_at), 0) FROM conversations"
                ).fetchone()
                if row is not None:
                    self._clock = max(self._clock, int(row[0]))
                conn.commit()
            finally:
                conn.close()
        except (OSError, sqlite3.Error) as exc:
            logger.warning("会话历史 SQLite 初始化失败，降级为内存模式：%s", exc)
            return None
        return path

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        """打开一次独立连接并设置 WAL / busy_timeout。"""

        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    def _next_updated_at(self) -> int:
        """返回严格递增的毫秒时间戳；调用方必须持有 ``_guard``。"""

        now = int(time.time() * 1000)
        self._clock = max(now, self._clock + 1)
        return self._clock

    def _db_get(self, path: Path, user_id: str) -> list[BaseMessage]:
        conn = self._connect(path)
        try:
            row = conn.execute(
                "SELECT history FROM conversations WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                return []
            # 读也刷新 updated_at，保持与内存模式一致的 LRU 语义
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE user_id = ?",
                (self._next_updated_at(), user_id),
            )
            conn.commit()
        finally:
            conn.close()

        try:
            return messages_from_dict(json.loads(row[0]))
        except (TypeError, ValueError, KeyError) as exc:
            # 单行数据损坏不应该拖垮整个存储，按空历史处理
            logger.warning(
                "会话历史反序列化失败，按空历史处理：user=%s err=%s", user_id, exc
            )
            return []

    def _db_set(self, path: Path, user_id: str, trimmed: list[BaseMessage]) -> None:
        conn = self._connect(path)
        try:
            if not trimmed:
                conn.execute(
                    "DELETE FROM conversations WHERE user_id = ?", (user_id,)
                )
                conn.commit()
                return

            payload = json.dumps(messages_to_dict(trimmed), ensure_ascii=False)
            conn.execute(
                _UPSERT_HISTORY,
                (user_id, payload, self._next_updated_at()),
            )
            conn.execute(_EVICT_USERS, (self._max_users,))
            conn.commit()
        finally:
            conn.close()

    def _degrade(self, exc: BaseException) -> None:
        """SQLite 出错后切回内存模式，调用方继续按内存语义工作。"""

        logger.warning("会话历史 SQLite 操作失败，降级为内存模式：%s", exc)
        self._db_path = None

    # ---- 内部：内存模式 ----

    def _memory_get(self, user_id: str) -> list[BaseMessage]:
        history = self._history.get(user_id)
        if not history:
            return []
        self._history.move_to_end(user_id)
        return list(history)

    def _memory_set(self, user_id: str, trimmed: list[BaseMessage]) -> None:
        if not trimmed:
            self._history.pop(user_id, None)
            return

        self._history[user_id] = trimmed
        self._history.move_to_end(user_id)
        while len(self._history) > self._max_users:
            # 只淘汰 history，不删 _locks。
            # lock_for() 返回锁后、acquire 前若该用户的锁被淘汰，会新建第二把锁，
            # 导致同一用户的消息失去互斥。每个用户一个 Lock 对象很小，保留即可。
            self._history.popitem(last=False)
