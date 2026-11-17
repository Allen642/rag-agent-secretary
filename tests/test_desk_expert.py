"""desk_expert 接入测试：工具集、自然语言时间、路由与图集成。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from secretary.demo_model import DemoChatModel
from secretary.desk import DeskStore, parse_natural
from secretary.graph import (
    WORKER_NODES,
    SecretaryAssistant,
    _route_from_supervisor,
    build_secretary_graph,
)
from secretary.rag import ChromaKnowledgeBase
from secretary.supervisor import RouteDecision
from secretary.tools import DESK_TOOL_NAMES, make_desk_tools
from secretary.workers import DeskExpert


@pytest.fixture()
def store(tmp_path) -> DeskStore:
    return DeskStore(tmp_path / "desk.db")


@pytest.fixture()
def desk_tools(store: DeskStore):
    return {tool_item.name: tool_item for tool_item in make_desk_tools(store)}


def test_make_desk_tools_returns_seven_named_tools(store: DeskStore) -> None:
    tools = make_desk_tools(store)

    assert len(tools) == 7
    assert [item.name for item in tools] == list(DESK_TOOL_NAMES)


def test_add_task_parses_natural_due_at(store: DeskStore, desk_tools) -> None:
    # 真实调用 parse_natural，验证工具层没有绕过自然语言解析
    before = int(parse_natural("明天下午3点").timestamp())
    result = desk_tools["add_task"].invoke(
        {"title": "交房租", "due_at": "明天下午3点"}
    )
    after = int(parse_natural("明天下午3点").timestamp())

    tasks = store.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].due_ts in {before, after}
    assert tasks[0].title == "交房租"
    # 返回必须带 id，LLM 后续完成/改期靠它定位
    assert tasks[0].id in result
    assert "已添加任务" in result
    assert "待办" in result

    # 同样的标题和截止时间再次添加，复用已有任务而不是新增
    desk_tools["add_task"].invoke({"title": "交房租", "due_at": "明天下午3点"})
    assert store.count() == 1


def test_update_task_clear_remind(store: DeskStore, desk_tools) -> None:
    task, _ = store.add_task(
        "交房租",
        due_at="2026-11-20 09:00",
        remind_minutes=60,
    )

    result = desk_tools["update_task"].invoke(
        {"task_id": task.id, "clear_remind": True}
    )

    updated = store.get_task(task.id)
    assert updated is not None
    assert updated.remind_minutes is None
    assert updated.due_ts == task.due_ts
    assert "已更新任务" in result


def test_update_task_conflicting_clear_returns_text(store: DeskStore, desk_tools) -> None:
    task, _ = store.add_task(
        "交房租",
        due_at="2026-11-20 09:00",
        remind_minutes=60,
    )

    # store 负责冲突校验，工具层只兜底成文本，不抛异常
    result = desk_tools["update_task"].invoke(
        {
            "task_id": task.id,
            "clear_remind": True,
            "remind_minutes": 30,
        }
    )

    assert "修改失败" in result
    updated = store.get_task(task.id)
    assert updated is not None
    assert updated.remind_minutes == 60


def test_desk_expert_name(store: DeskStore) -> None:
    expert = DeskExpert(DemoChatModel(), make_desk_tools(store))

    assert expert.name == "desk_expert"


def test_route_decision_accepts_desk_expert() -> None:
    decision = RouteDecision(next_agent="desk_expert", reason="涉及待办提醒")

    assert decision.next_agent == "desk_expert"

    with pytest.raises(ValidationError):
        RouteDecision(next_agent="memory_curator", reason="非法节点")


def test_graph_contains_desk_expert_and_routes_to_it(tmp_path) -> None:
    graph = build_secretary_graph(
        DemoChatModel(),
        knowledge_base=ChromaKnowledgeBase(
            persist_dir=tmp_path / "chroma",
            collection_name="desk_expert_test",
        ),
        desk_store=DeskStore(tmp_path / "desk.db"),
        enable_auto_memory=False,
    )

    assert "desk_expert" in WORKER_NODES
    graph_nodes = graph.get_graph().nodes
    for node in WORKER_NODES:
        assert node in graph_nodes

    assert _route_from_supervisor({"next_agent": "desk_expert"}) == "desk_expert"
    assert _route_from_supervisor({"next_agent": "unknown"}) == "FINISH"


def test_offline_graph_answers_desk_question(tmp_path) -> None:
    assistant = SecretaryAssistant(
        llm=DemoChatModel(),
        knowledge_base=ChromaKnowledgeBase(
            persist_dir=tmp_path / "chroma",
            collection_name="desk_expert_demo",
        ),
        desk_store=DeskStore(tmp_path / "desk.db"),
        enable_auto_memory=False,
    )

    answer = assistant.ask("提醒我明天下午3点交房租")

    assert "已调用 list_tasks，链路打通" in answer
