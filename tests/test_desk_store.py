"""DeskStore 工作台存储测试。"""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from secretary.desk import DEFAULT_DESK_DB, DeskStore, Task
from secretary.desk.models import (
    is_date_only,
    parse_timestamp,
    to_local_iso,
)

# scripts 不是包，按文件路径加载 CLI 模块做冒烟测试
_CLI_PATH = Path(__file__).resolve().parents[1] / "scripts" / "desk.py"
_SPEC = importlib.util.spec_from_file_location("desk_cli_for_test", _CLI_PATH)
assert _SPEC is not None and _SPEC.loader is not None
desk_cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(desk_cli)


@pytest.fixture()
def store(tmp_path) -> DeskStore:
    # 顺便验证：多级父目录不存在时自动创建
    return DeskStore(tmp_path / "runtime" / "desk.db")


def _mark_reminded(store: DeskStore, task_id: str, ts: int = 1_700_000_000) -> None:
    """直接改库模拟「提醒已经发出」，测试 reopen/update 会清 reminded_ts。"""

    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute("UPDATE tasks SET reminded_ts = ? WHERE id = ?", (ts, task_id))
        conn.commit()
    finally:
        conn.close()


def test_add_and_get_task(store: DeskStore) -> None:
    due = datetime(2026, 11, 20, 9, 0)
    task, created = store.add_task(
        "  交房租  ",
        due_at=due,
        remind_minutes=60,
        priority="high",
        tags=["生活", " 生活 ", "", "账单"],
        notes="  银行转账  ",
    )

    assert created is True
    assert len(task.id) == 32
    assert task.title == "交房租"
    assert task.status == "open"
    assert task.priority == "high"
    assert task.due_ts == int(due.timestamp())
    assert task.remind_minutes == 60
    assert task.tags == ["生活", "账单"]
    assert task.notes == "银行转账"
    assert task.created_ts > 0
    assert task.updated_ts == task.created_ts
    assert task.completed_ts is None
    assert task.reminded_ts is None
    assert to_local_iso(task.due_ts) == "2026-11-20 09:00:00"

    fetched = store.get_task(task.id)
    assert fetched == task
    assert store.get_task("不存在的id") is None


def test_db_path_is_absolute_and_wal_enabled(store: DeskStore) -> None:
    assert store.db_path.is_absolute()
    assert store.db_path.exists()
    assert str(DEFAULT_DESK_DB).endswith("desk.db")

    with sqlite3.connect(store.db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_list_orders_by_due_with_none_last(store: DeskStore) -> None:
    store.add_task("无截止")
    store.add_task("晚一点", due_at="2026-11-22 09:00")
    store.add_task("早一点", due_at="2026-11-20 09:00")

    tasks = store.list_tasks()

    assert [task.title for task in tasks] == ["早一点", "晚一点", "无截止"]
    assert all(task.id for task in tasks)


def test_status_filter_and_include_done(store: DeskStore) -> None:
    keep, _ = store.add_task("待办事项")
    done, _ = store.add_task("已完成事项")
    cancelled, _ = store.add_task("已取消事项")
    store.complete_task(done.id)
    store.cancel_task(cancelled.id)

    assert [task.id for task in store.list_tasks()] == [keep.id]
    assert {task.id for task in store.list_tasks(include_done=True)} == {
        keep.id,
        done.id,
    }
    assert [task.id for task in store.list_tasks(status="done")] == [done.id]
    assert [task.id for task in store.list_tasks(status="cancelled")] == [cancelled.id]
    assert [task.id for task in store.list_tasks(status="open")] == [keep.id]


def test_query_matches_title_and_notes_literally(store: DeskStore) -> None:
    percent, _ = store.add_task("复习 100% 高数")
    underscore, _ = store.add_task("订机票", notes="确认 a_b 窗口")

    assert [task.id for task in store.list_tasks(query="100%")] == [percent.id]
    assert [task.id for task in store.list_tasks(query="复习")] == [percent.id]
    assert [task.id for task in store.list_tasks(query="a_b")] == [underscore.id]
    assert store.list_tasks(query="a%b") == []
    assert store.list_tasks(query="%") == [percent]
    # 反斜杠本身也要按字面量处理，不能把后面的字符当转义吞掉
    assert store.list_tasks(query="\\") == []


def test_list_tasks_rejects_non_string_query(store: DeskStore) -> None:
    with pytest.raises(ValueError):
        store.list_tasks(query=123)  # type: ignore[arg-type]


def test_list_between_boundaries_and_order(store: DeskStore) -> None:
    store.add_task("之前", due_at="2026-11-20 08:59")
    store.add_task("正好起点", due_at="2026-11-20 09:00")
    store.add_task("中间", due_at="2026-11-20 12:00")
    store.add_task("正好终点前", due_at="2026-11-20 17:59")
    store.add_task("正好终点", due_at="2026-11-20 18:00")
    store.add_task("没有截止时间")

    start = int(datetime(2026, 11, 20, 9, 0).timestamp())
    end = int(datetime(2026, 11, 20, 18, 0).timestamp())

    tasks = store.list_between(start, end)

    # 含左、不含右；无截止时间不参与；按 due_ts 升序
    assert [task.title for task in tasks] == ["正好起点", "中间", "正好终点前"]
    assert store.list_between(start, start) == []


def test_list_between_include_done_and_excludes_cancelled(store: DeskStore) -> None:
    open_task, _ = store.add_task("待办", due_at="2026-11-20 10:00")
    done_task, _ = store.add_task("已完成", due_at="2026-11-20 11:00")
    cancelled_task, _ = store.add_task("已取消", due_at="2026-11-20 12:00")
    store.complete_task(done_task.id)
    store.cancel_task(cancelled_task.id)

    start = int(datetime(2026, 11, 20, 0, 0).timestamp())
    end = int(datetime(2026, 11, 21, 0, 0).timestamp())

    assert [task.id for task in store.list_between(start, end)] == [open_task.id]
    assert [task.id for task in store.list_between(start, end, include_done=True)] == [
        open_task.id,
        done_task.id,
    ]


def test_list_between_rejects_invalid_range(store: DeskStore) -> None:
    start = int(datetime(2026, 11, 20, 9, 0).timestamp())
    end = int(datetime(2026, 11, 20, 18, 0).timestamp())

    with pytest.raises(ValueError):
        store.list_between(end, start)
    with pytest.raises(ValueError):
        store.list_between("1800000000", end)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        store.list_between(start, True)  # type: ignore[arg-type]


def test_list_overdue_filters_and_orders(store: DeskStore) -> None:
    store.add_task("更早过期", due_at="2026-11-19 09:00")
    store.add_task("过期", due_at="2026-11-20 09:00")
    store.add_task("正好现在", due_at="2026-11-20 12:00")
    store.add_task("未来", due_at="2026-11-20 18:00")
    store.add_task("没有截止时间")
    done, _ = store.add_task("已完成但过期", due_at="2026-11-18 09:00")
    store.complete_task(done.id)

    now = int(datetime(2026, 11, 20, 12, 0).timestamp())

    assert [task.title for task in store.list_overdue(now)] == ["更早过期", "过期"]
    assert [task.title for task in store.list_overdue(now, include_done=True)] == [
        "已完成但过期",
        "更早过期",
        "过期",
    ]


def test_list_overdue_validates_now_ts(store: DeskStore) -> None:
    with pytest.raises(ValueError):
        store.list_overdue("1800000000")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        store.list_overdue(True)  # type: ignore[arg-type]


def test_complete_reopen_cancel_transitions(store: DeskStore) -> None:
    task, _ = store.add_task("写周报", due_at="2026-11-20 09:00")

    completed = store.complete_task(task.id)
    assert completed.status == "done"
    assert completed.completed_ts is not None
    # 幂等：重复完成不刷新时间
    assert store.complete_task(task.id).completed_ts == completed.completed_ts

    _mark_reminded(store, task.id)
    reopened = store.reopen_task(task.id)
    assert reopened.status == "open"
    assert reopened.completed_ts is None
    assert reopened.reminded_ts is None
    assert store.reopen_task(task.id).status == "open"

    cancelled = store.cancel_task(task.id)
    assert cancelled.status == "cancelled"
    assert store.cancel_task(task.id).status == "cancelled"

    with pytest.raises(ValueError):
        store.complete_task(task.id)


def test_soft_delete_hides_task_but_keeps_row(store: DeskStore) -> None:
    task, _ = store.add_task("临时任务")
    store.delete_task(task.id)

    remaining = store.get_task(task.id)
    assert remaining is not None
    assert remaining.status == "cancelled"
    assert store.list_tasks() == []
    assert store.list_tasks(include_done=True) == []
    assert [item.id for item in store.list_tasks(status="cancelled")] == [task.id]
    assert store.count() == 1
    assert store.count(status="open") == 0


def test_hard_delete_removes_row(store: DeskStore) -> None:
    task, _ = store.add_task("要删除的任务")
    store.delete_task(task.id, hard=True)

    assert store.get_task(task.id) is None
    assert store.list_tasks(status="cancelled") == []
    assert store.count() == 0

    with pytest.raises(KeyError):
        store.delete_task(task.id, hard=True)


def test_second_instance_sees_persisted_data(tmp_path) -> None:
    db_path = tmp_path / "desk.db"
    first = DeskStore(db_path)
    task, _ = first.add_task("重启后还在", due_at="2026-11-20 09:00", tags=["生活"])

    second = DeskStore(db_path)
    tasks = second.list_tasks()

    assert [item.id for item in tasks] == [task.id]
    assert tasks[0].tags == ["生活"]
    assert tasks[0].due_ts == task.due_ts


def test_dedupe_default_rule(store: DeskStore) -> None:
    first, created_first = store.add_task(
        "买菜",
        due_at="2026-11-20 18:00",
        priority="high",
        tags=["生活"],
        remind_minutes=10,
    )
    assert created_first is True

    # 同标题 + 同 due：返回已有任务，且不覆盖本次传入的字段
    same, created_same = store.add_task(
        "买菜",
        due_at="2026-11-20 18:00",
        priority="low",
        tags=["工作"],
        remind_minutes=99,
        notes="不该覆盖",
    )
    assert created_same is False
    assert same.id == first.id
    assert same.priority == "high"
    assert same.tags == ["生活"]
    assert same.remind_minutes == 10
    assert same.notes == ""
    assert same.updated_ts == first.updated_ts

    different_due, created_different_due = store.add_task(
        "买菜", due_at="2026-11-21 18:00"
    )
    assert created_different_due is True
    assert different_due.id != first.id

    # 无截止时间的任务也按「同标题 + 同 None」去重
    no_due, _ = store.add_task("随手记")
    same_no_due, created_same_no_due = store.add_task("随手记")
    assert created_same_no_due is False
    assert same_no_due.id == no_due.id

    store.complete_task(first.id)
    recreated, created_recreated = store.add_task(
        "买菜", due_at="2026-11-20 18:00"
    )
    assert created_recreated is True
    assert recreated.id != first.id


def test_dedupe_key_overrides_default_rule(store: DeskStore) -> None:
    keyed, created_keyed = store.add_task(
        "每日打卡", due_at="2026-11-20 08:00", dedupe_key="habit-checkin"
    )
    assert created_keyed is True

    # 标题和截止时间都不同，但 key 相同 -> 复用
    same_key, created_same_key = store.add_task(
        "每日打卡（第二次）", due_at="2026-11-21 08:00", dedupe_key="habit-checkin"
    )
    assert created_same_key is False
    assert same_key.id == keyed.id

    # key 不同则不复用，即使标题和截止时间一样
    other_key, created_other_key = store.add_task(
        "每日打卡", due_at="2026-11-20 08:00", dedupe_key="another-key"
    )
    assert created_other_key is True
    assert other_key.id != keyed.id

    # 空串视为未提供，回落到默认规则
    blank_one, created_blank_one = store.add_task(
        "空key任务", due_at="2026-11-20 10:00", dedupe_key=""
    )
    blank_two, created_blank_two = store.add_task(
        "空key任务", due_at="2026-11-20 10:00", dedupe_key=""
    )
    assert created_blank_one is True
    assert created_blank_two is False
    assert blank_two.id == blank_one.id


@pytest.mark.parametrize(
    "kwargs",
    [
        {"priority": "urgent"},
        {"due_at": "明天下午"},
        {"due_at": ""},
        {"remind_minutes": 30},
        {"due_at": "2026-11-20 09:00", "remind_minutes": -5},
        {"tags": "单个字符串"},
    ],
)
def test_add_task_rejects_invalid_input(store: DeskStore, kwargs) -> None:
    with pytest.raises(ValueError):
        store.add_task("周末计划", **kwargs)


def test_empty_title_is_rejected(store: DeskStore) -> None:
    with pytest.raises(ValueError):
        store.add_task("   ")
    with pytest.raises(ValueError):
        store.add_task(123)  # type: ignore[arg-type]


def test_invalid_list_and_count_params(store: DeskStore) -> None:
    with pytest.raises(ValueError):
        store.list_tasks(limit=0)
    with pytest.raises(ValueError):
        store.list_tasks(status="bogus")
    with pytest.raises(ValueError):
        store.count(status="bogus")


@pytest.mark.parametrize(
    "operation",
    ["complete", "reopen", "cancel", "update", "delete"],
)
def test_missing_id_raises_key_error(store: DeskStore, operation: str) -> None:
    with pytest.raises(KeyError):
        if operation == "delete":
            store.delete_task("missing")
        else:
            getattr(store, f"{operation}_task")("missing")


def test_update_task_partial_fields(store: DeskStore) -> None:
    task, _ = store.add_task("旧标题", due_at="2026-11-20 09:00", tags=["旧标签"])

    updated = store.update_task(
        task.id,
        title="新标题",
        priority="high",
        tags=["新标签"],
        notes="补充说明",
    )

    assert updated.title == "新标题"
    assert updated.priority == "high"
    assert updated.tags == ["新标签"]
    assert updated.notes == "补充说明"
    assert updated.due_ts == task.due_ts
    assert updated.updated_ts >= task.updated_ts

    cleared = store.update_task(task.id, tags=())
    assert cleared.tags == []


def test_update_due_or_remind_resets_reminded_ts(store: DeskStore) -> None:
    task, _ = store.add_task("喝水", due_at="2026-11-20 09:00", remind_minutes=10)

    _mark_reminded(store, task.id)
    changed_due = store.update_task(task.id, due_at="2026-11-20 10:00")
    assert changed_due.due_ts == parse_timestamp("2026-11-20 10:00")
    assert changed_due.reminded_ts is None

    _mark_reminded(store, task.id)
    changed_remind = store.update_task(task.id, remind_minutes=20)
    assert changed_remind.remind_minutes == 20
    assert changed_remind.reminded_ts is None


def test_update_clear_due_and_clear_remind(store: DeskStore) -> None:
    task, _ = store.add_task("体检", due_at="2026-11-20 09:00", remind_minutes=30)

    _mark_reminded(store, task.id)
    cleared_remind = store.update_task(task.id, clear_remind=True)
    assert cleared_remind.remind_minutes is None
    assert cleared_remind.due_ts == task.due_ts
    assert cleared_remind.reminded_ts is None

    cleared_due = store.update_task(task.id, clear_due=True)
    assert cleared_due.due_ts is None
    assert cleared_due.remind_minutes is None
    assert cleared_due.reminded_ts is None


def test_update_conflicting_params_raise(store: DeskStore) -> None:
    task, _ = store.add_task("冲突检查", due_at="2026-11-20 09:00", remind_minutes=5)

    with pytest.raises(ValueError):
        store.update_task(task.id, due_at="2026-11-21 09:00", clear_due=True)
    with pytest.raises(ValueError):
        store.update_task(task.id, remind_minutes=10, clear_remind=True)
    with pytest.raises(ValueError):
        store.update_task(task.id, remind_minutes=10, clear_due=True)
    with pytest.raises(ValueError):
        store.update_task(task.id, title="   ")

    no_due, _ = store.add_task("没有截止时间")
    with pytest.raises(ValueError):
        store.update_task(no_due.id, remind_minutes=10)


def test_count_includes_all_rows_when_status_is_none(store: DeskStore) -> None:
    open_task, _ = store.add_task("待办")
    done_task, _ = store.add_task("已完成")
    cancelled_task, _ = store.add_task("已取消")
    store.complete_task(done_task.id)
    store.cancel_task(cancelled_task.id)

    assert store.count() == 3
    assert store.count(status="open") == 1
    assert store.count(status="done") == 1
    assert store.count(status="cancelled") == 1
    assert store.get_task(open_task.id) is not None


def test_parse_timestamp_variants() -> None:
    assert parse_timestamp(None) is None
    assert parse_timestamp("2026-11-20 09:30") == int(
        datetime(2026, 11, 20, 9, 30).timestamp()
    )
    assert parse_timestamp("2026-11-20T09:30:00") == parse_timestamp("2026-11-20 09:30")
    # 只写日期 -> 本地 18:00
    assert parse_timestamp("2026-11-20") == int(datetime(2026, 11, 20, 18, 0).timestamp())
    # 带时区的 datetime 按其时区换算
    aware = datetime(2026, 11, 20, 9, 0, tzinfo=timezone(timedelta(hours=8)))
    assert parse_timestamp(aware) == int(aware.timestamp())
    assert is_date_only("2026-11-20") is True
    assert is_date_only("2026-11-20 09:00") is False

    for bad in ("2026-13-01", "2026-11-20 25:00", "明天下午", "", 123):
        with pytest.raises(ValueError):
            parse_timestamp(bad)


def test_task_to_dict_exposes_iso_time(store: DeskStore) -> None:
    task, _ = store.add_task("导出", due_at="2026-11-20 09:00")
    payload = task.to_dict()

    assert payload["id"] == task.id
    assert payload["due_at"] == "2026-11-20 09:00:00"
    assert payload["due_ts"] == task.due_ts
    assert isinstance(payload["tags"], list)
    assert isinstance(task, Task)


def test_cli_db_flag_works_before_and_after_command(tmp_path, capsys) -> None:
    db_path = tmp_path / "cli.db"

    assert desk_cli.main(["--db", str(db_path), "add", "写测试"]) == 0
    assert desk_cli.main(["add", "再看一眼", "--db", str(db_path)]) == 0
    capsys.readouterr()

    assert desk_cli.main(["--db", str(db_path), "list"]) == 0
    output = capsys.readouterr().out
    assert "写测试" in output
    assert "再看一眼" in output
    assert "共 2 条" in output


def test_cli_reports_dedupe_reuse(tmp_path, capsys) -> None:
    db_path = tmp_path / "cli.db"

    assert desk_cli.main(["--db", str(db_path), "add", "重复任务", "--due", "2026-11-20 09:00"]) == 0
    assert "已添加：" in capsys.readouterr().out

    assert desk_cli.main(["--db", str(db_path), "add", "重复任务", "--due", "2026-11-20 09:00"]) == 0
    assert "已存在，复用：" in capsys.readouterr().out


def test_cli_reports_errors_without_traceback(tmp_path, capsys) -> None:
    db_path = tmp_path / "cli.db"

    assert desk_cli.main(["--db", str(db_path), "add", "无时间提醒", "--remind", "30"]) == 1
    assert "remind_minutes 必须配合 due_at 使用" in capsys.readouterr().err

    assert desk_cli.main(["--db", str(db_path), "done", "missing-id"]) == 1
    assert "任务不存在" in capsys.readouterr().err
