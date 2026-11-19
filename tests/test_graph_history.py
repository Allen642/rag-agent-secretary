"""短期上下文测试：ask_turn 返回可直接复用的干净历史。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from secretary.demo_model import DemoChatModel
from secretary.desk import DeskStore
from secretary.graph import SecretaryAssistant
from secretary.rag import ChromaKnowledgeBase


def test_conversation_history_keeps_only_user_and_final_worker_replies() -> None:
    state = {
        "messages": [
            HumanMessage(content="第一轮"),
            AIMessage(
                content="",
                name="desk_expert",
                tool_calls=[
                    {
                        "name": "list_tasks",
                        "args": {},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(content="工具结果", tool_call_id="call-1", name="list_tasks"),
            AIMessage(content="第一轮回答", name="desk_expert"),
            # Supervisor 的 JSON 没有 Worker 名字，应被过滤
            AIMessage(content='{"next_agent": "FINISH"}'),
            HumanMessage(content="第二轮"),
            AIMessage(
                content="",
                name="study_expert",
                tool_calls=[
                    {
                        "name": "search_knowledge_base",
                        "args": {"query": "极限"},
                        "id": "call-2",
                        "type": "tool_call",
                    }
                ],
            ),
        ]
    }

    history = SecretaryAssistant._conversation_history(state)

    assert [message.content for message in history] == [
        "第一轮",
        "第一轮回答",
        "第二轮",
    ]
    assert all(
        not getattr(message, "tool_calls", None)
        for message in history
        if isinstance(message, AIMessage)
    )


def test_conversation_history_limits_to_recent_messages() -> None:
    messages = []
    for index in range(15):
        messages.append(HumanMessage(content=f"u{index}"))
        messages.append(AIMessage(content=f"a{index}", name="study_expert"))

    history = SecretaryAssistant._conversation_history({"messages": messages}, limit=4)

    assert [message.content for message in history] == ["u13", "a13", "u14", "a14"]


def test_ask_turn_returns_answer_and_clean_history(tmp_path) -> None:
    assistant = SecretaryAssistant(
        llm=DemoChatModel(),
        knowledge_base=ChromaKnowledgeBase(
            persist_dir=tmp_path / "chroma",
            collection_name="graph_history_test",
        ),
        desk_store=DeskStore(tmp_path / "desk.db"),
        enable_auto_memory=False,
    )

    answer, history = assistant.ask_turn("提醒我明天下午3点交房租")

    assert "已调用 list_tasks" in answer
    assert len(history) == 2
    assert isinstance(history[0], HumanMessage)
    assert isinstance(history[1], AIMessage)
    assert history[1].name == "desk_expert"
    assert not history[1].tool_calls

    second_answer, second_history = assistant.ask_turn("那明天呢", history=history)

    assert second_answer
    assert len(second_history) == 4
    assert [message.content for message in second_history[:2]] == [
        message.content for message in history
    ]
    assert all(
        not getattr(message, "tool_calls", None)
        for message in second_history
        if isinstance(message, AIMessage)
    )
