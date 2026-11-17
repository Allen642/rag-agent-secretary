"""提醒查询、通知入库与 daemon 单步函数的测试。"""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest

from secretary.desk import DeskStore, Notification

# scripts 不是包，按文件路径加载 daemon 模块
_DAEMON_PATH = Path(__file__).resolve().parents[1] / "scripts" / "remind_daemon.py"
_SPEC = importlib.util.spec_from_file_location("remind_daemon_for_test", _DAEMON_PATH)
assert _SPEC is not None and _SPEC.loader is not None
remind_daemon = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(remind_daemon)


@pytest.fixture()
def store(tmp_path) -> DeskStore:
    return DeskStore(tmp_path / "runtime" / "desk.db")


def _ts(hour: int, minute: int = 0, *, day: int = 18) -> int:
    return int(datetime(2026, 11, day, hour, minute).timestamp())


# ---------------------------------------------------------------- list_due


def test_list_due_filters_and_orders_by_trigger(store: DeskStore) -> None:
    later = store.add_task(
        "较晚触发", due_at=datetime(2026, 11, 18, 16, 0), remind_minutes=60
    )[0]
    earlier = store.add_task(
        "较早触发", due_at=datetime(2026, 11, 18, 15, 0), remind_minutes=30
    )[0]

    # 14:29 时两个都还没到触发时间
    assert store.list_due(_ts(14, 29)) == []
    # 14:30 时较早的那条到点
    assert [task.id for task in store.list_due(_ts(14, 30))] == [earlier.id]
    # 15:00 时两条都到点，按触发时间升序
    assert [task.id for task in store.list_due(_ts(15, 0))] == [
        earlier.id,
        later.id,
    ]


def test_list_due_excludes_other_states_and_reminded(store: DeskStore) -> None:
    due = datetime(2026, 11, 18, 15, 0)
    keep = store.add_task("正常提醒", due_at=due, remind_minutes=30)[0]
    store.add_task("没有提醒", due_at=due)
    done = store.add_task("已完成", due_at=due, remind_minutes=30)[0]
    cancelled = store.add_task("已取消", due_at=due, remind_minutes=30)[0]
    store.complete_task(done.id)
    store.cancel_task(cancelled.id)

    persisted = store.add_task("已经提醒过", due_at=due, remind_minutes=30)[0]
    assert store.mark_reminded(persisted.id, _ts(14, 0)) is True

    assert [task.id for task in store.list_due(_ts(14, 30))] == [keep.id]


def test_list_due_validates_now_ts(store: DeskStore) -> None:
    with pytest.raises(ValueError):
        store.list_due("1800000000")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        store.list_due(True)  # type: ignore[arg-type]


# ------------------------------------------------------------ mark_reminded


def test_mark_reminded_is_idempotent(store: DeskStore) -> None:
    task = store.add_task(
        "只提醒一次", due_at=datetime(2026, 11, 18, 15, 0), remind_minutes=30
    )[0]
    ts = _ts(14, 30)

    assert store.mark_reminded(task.id, ts) is True
    assert store.mark_reminded(task.id, ts + 10) is False
    assert store.get_task(task.id).reminded_ts == ts
    # 不存在或已提醒都返回 False，不抛 KeyError
    assert store.mark_reminded("missing", ts) is False


def test_mark_reminded_validates_ts(store: DeskStore) -> None:
    task = store.add_task("校验", due_at=datetime(2026, 11, 18, 15, 0))[0]
    with pytest.raises(ValueError):
        store.mark_reminded(task.id, 1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        store.mark_reminded(task.id, -1)


# ------------------------------------------------------------ notifications


def test_notification_write_and_unread_read_are_separate(store: DeskStore) -> None:
    first = store.add_notification(
        kind="remind",
        title="交房租",
        body="截止：2026-11-20 09:00",
        task_id="task-1",
        due_ts=_ts(9, 0, day=20),
        created_ts=_ts(8, 0, day=20),
    )
    second = store.add_notification(
        kind="missed",
        title="错过的提醒（1 条）",
        body="- 交房租",
        created_ts=_ts(8, 1, day=20),
    )

    assert isinstance(first, Notification)
    assert first.id > 0
    assert first.task_id == "task-1"
    assert first.read_ts is None
    assert first.created_at == "2026-11-20 08:00:00"
    assert second.task_id is None

    unread = store.list_unread_notifications()
    assert [item.id for item in unread] == [second.id, first.id]
    assert store.count_unread_notifications() == 2

    # 标记已读：条件更新，第二次返回 False
    assert store.mark_notification_read(first.id, ts=_ts(9, 0)) is True
    assert store.mark_notification_read(first.id, ts=_ts(9, 1)) is False
    assert store.count_unread_notifications() == 1
    assert [item.id for item in store.list_unread_notifications()] == [second.id]

    # list_notifications 能看到全部；默认按创建时间倒序
    assert [item.id for item in store.list_notifications()] == [second.id, first.id]
    assert store.list_notifications(limit=1)[0].id == second.id


def test_notification_validation(store: DeskStore) -> None:
    with pytest.raises(ValueError):
        store.add_notification(kind="other", title="未知类型")
    with pytest.raises(ValueError):
        store.add_notification(kind="remind", title="   ")
    with pytest.raises(ValueError):
        store.add_notification(kind="remind", title="坏时间", due_ts="123")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        store.list_notifications(limit=0)
    with pytest.raises(ValueError):
        store.mark_notification_read("1")  # type: ignore[arg-type]

    assert store.count_unread_notifications() == 0


def test_notifications_survive_restart(tmp_path) -> None:
    db_path = tmp_path / "desk.db"
    first = DeskStore(db_path)
    created = first.add_notification(kind="remind", title="重启后还在")

    second = DeskStore(db_path)
    assert [item.id for item in second.list_unread_notifications()] == [created.id]


# ------------------------------------------------------------------- _tick


def test_tick_writes_reminder_and_is_restart_safe(store: DeskStore) -> None:
    now_ts = _ts(14, 30)
    task = store.add_task(
        "开会", due_at=datetime(2026, 11, 18, 15, 0), remind_minutes=30
    )[0]

    handled = remind_daemon._tick(store, now_ts)

    assert [item.id for item in handled] == [task.id]
    assert store.get_task(task.id).reminded_ts == now_ts
    notifications = store.list_unread_notifications()
    assert len(notifications) == 1
    assert notifications[0].kind == "remind"
    assert notifications[0].task_id == task.id
    assert notifications[0].title == "开会"

    # 再跑一轮：不会重复写通知
    assert remind_daemon._tick(store, now_ts + 30) == []
    assert store.count_unread_notifications() == 1


def test_tick_prints_reminder_to_terminal(store: DeskStore, capsys) -> None:
    store.add_task(
        "吃药", due_at=datetime(2026, 11, 18, 15, 0), remind_minutes=30
    )

    remind_daemon._tick(store, _ts(14, 30))

    output = capsys.readouterr().out
    assert "[提醒] 吃药" in output
    assert "2026-11-18 15:00:00" in output


def test_startup_catch_up_merges_missed(store: DeskStore, capsys) -> None:
    now_ts = _ts(14, 0)
    first = store.add_task(
        "早就过期A", due_at=datetime(2026, 11, 18, 10, 0), remind_minutes=10
    )[0]
    second = store.add_task(
        "早就过期B", due_at=datetime(2026, 11, 18, 11, 0), remind_minutes=10
    )[0]

    handled = remind_daemon._tick(store, now_ts, catch_up=True)

    assert {item.id for item in handled} == {first.id, second.id}
    notifications = store.list_unread_notifications()
    assert len(notifications) == 1
    assert notifications[0].kind == "missed"
    assert notifications[0].title == "错过的提醒（2 条）"
    assert "早就过期A" in notifications[0].body
    assert "早就过期B" in notifications[0].body
    assert store.count_unread_notifications() == 1
    assert "[补发] 错过的提醒（2 条）" in capsys.readouterr().out

    # 重启后再次补发：没有可补的，不重复写
    assert remind_daemon._tick(store, now_ts + 60, catch_up=True) == []
    assert store.count_unread_notifications() == 1


def test_startup_catch_up_keeps_future_due_as_normal_reminder(
    store: DeskStore,
) -> None:
    now_ts = _ts(14, 0)
    overdue = store.add_task(
        "已经过期", due_at=datetime(2026, 11, 18, 13, 0), remind_minutes=60
    )[0]
    upcoming = store.add_task(
        "还没到期", due_at=datetime(2026, 11, 18, 15, 0), remind_minutes=60
    )[0]

    handled = remind_daemon._tick(store, now_ts, catch_up=True)

    assert {item.id for item in handled} == {overdue.id, upcoming.id}
    kinds = {item.kind for item in store.list_notifications()}
    assert kinds == {"missed", "remind"}
    assert store.get_task(upcoming.id).reminded_ts == now_ts


# --------------------------------------------------------------------- main


def test_main_once_writes_missed_and_restart_does_not_repeat(
    tmp_path, capsys
) -> None:
    db_path = tmp_path / "daemon.db"
    store = DeskStore(db_path)
    # 用 2020 年保证无论当前时刻如何都算「已过期」
    store.add_task(
        "过期任务", due_at=datetime(2020, 1, 1, 9, 0), remind_minutes=30
    )

    assert remind_daemon.main(["--db", str(db_path), "--once"]) == 0
    first_output = capsys.readouterr().out
    assert "错过的提醒（1 条）" in first_output
    assert store.count_unread_notifications() == 1

    # 模拟重启：同样的库再跑一次，不应重复弹
    assert remind_daemon.main(["--db", str(db_path), "--once"]) == 0
    second_output = capsys.readouterr().out
    assert "错过的提醒" not in second_output
    assert store.count_unread_notifications() == 1


def test_main_rejects_non_positive_interval(tmp_path, capsys) -> None:
    assert remind_daemon.main(["--interval", "0", "--once"]) == 1
    assert "必须大于 0" in capsys.readouterr().err
