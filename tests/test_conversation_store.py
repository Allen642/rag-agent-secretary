"""ConversationStore 内存模式与 SQLite 持久化模式的单元测试。"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage

from secretary.conversation_store import ConversationStore


def _contents(history) -> list[str]:
    return [str(message.content) for message in history]


# ---- 内存模式 ----


def test_memory_store_trims_to_max_messages() -> None:
    store = ConversationStore(max_messages=3, max_users=10)
    store.set("u1", [HumanMessage(content=str(index)) for index in range(5)])

    assert _contents(store.get("u1")) == ["2", "3", "4"]


def test_memory_store_evicts_lru_and_get_refreshes_order() -> None:
    store = ConversationStore(max_messages=5, max_users=2)
    store.set("a", [HumanMessage(content="a")])
    store.set("b", [HumanMessage(content="b")])

    # 读 a 会刷新 LRU，随后写入 c 时应淘汰最久未使用的 b
    assert _contents(store.get("a")) == ["a"]
    store.set("c", [HumanMessage(content="c")])

    assert _contents(store.get("a")) == ["a"]
    assert _contents(store.get("c")) == ["c"]
    assert store.get("b") == []


def test_memory_store_keeps_lock_after_eviction() -> None:
    store = ConversationStore(max_messages=2, max_users=1)
    lock_a = store.lock_for("a")
    store.set("a", [HumanMessage(content="a")])
    # 写入 b 会把 a 的 history 挤掉，但锁对象必须保留
    store.set("b", [HumanMessage(content="b")])

    assert store.lock_for("a") is lock_a
    assert store.get("a") == []


# ---- SQLite 模式 ----


def test_sqlite_roundtrip_preserves_message_types_and_name(tmp_path) -> None:
    store = ConversationStore(db_path=tmp_path / "conversations.db")
    store.set(
        "u1",
        [
            HumanMessage(content="今天要做什么？"),
            AIMessage(content="先看日程。", name="desk_expert"),
        ],
    )

    restored = store.get("u1")

    assert [type(message) for message in restored] == [HumanMessage, AIMessage]
    assert _contents(restored) == ["今天要做什么？", "先看日程。"]
    assert restored[1].name == "desk_expert"


def test_sqlite_history_survives_new_instance(tmp_path) -> None:
    db_path = tmp_path / "conversations.db"
    first = ConversationStore(db_path=db_path)
    first.set("u1", [HumanMessage(content="重启前")])
    del first

    # 模拟进程重启：同一路径重新建实例，历史仍然可读
    second = ConversationStore(db_path=db_path)
    assert _contents(second.get("u1")) == ["重启前"]


def test_sqlite_store_trims_to_max_messages(tmp_path) -> None:
    store = ConversationStore(
        db_path=tmp_path / "conversations.db", max_messages=3, max_users=10
    )
    store.set("u1", [HumanMessage(content=str(index)) for index in range(5)])

    assert _contents(store.get("u1")) == ["2", "3", "4"]


def test_sqlite_store_evicts_oldest_user_beyond_max_users(tmp_path) -> None:
    store = ConversationStore(
        db_path=tmp_path / "conversations.db", max_messages=5, max_users=2
    )
    store.set("a", [HumanMessage(content="a")])
    store.set("b", [HumanMessage(content="b")])
    store.set("c", [HumanMessage(content="c")])

    assert store.get("a") == []
    assert _contents(store.get("b")) == ["b"]
    assert _contents(store.get("c")) == ["c"]


def test_sqlite_empty_history_removes_user(tmp_path) -> None:
    store = ConversationStore(db_path=tmp_path / "conversations.db")
    store.set("u1", [HumanMessage(content="a")])
    store.set("u1", [])

    assert store.get("u1") == []


def test_sqlite_unknown_user_returns_empty_list(tmp_path) -> None:
    store = ConversationStore(db_path=tmp_path / "conversations.db")

    assert store.get("nobody") == []


def test_sqlite_init_failure_degrades_to_memory(tmp_path, caplog) -> None:
    # 用一个普通文件冒充目录，让建库在 mkdir 阶段失败
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    db_path = blocker / "conversations.db"

    with caplog.at_level(logging.WARNING, logger="conversation_store"):
        store = ConversationStore(db_path=db_path)

    assert "降级" in caplog.text
    # 降级后仍按内存模式工作
    store.set("u1", [HumanMessage(content="降级后仍可用")])
    assert _contents(store.get("u1")) == ["降级后仍可用"]
