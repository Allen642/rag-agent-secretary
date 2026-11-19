"""用户偏好注入测试：长期记忆 -> 图状态 -> Worker system prompt。"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from secretary.graph import SecretaryAssistant
from secretary.rag.chroma_store import RetrievedChunk
from secretary.workers.base import WorkerAgent


class _RecordingChatModel(BaseChatModel):
    """不联网的测试模型：记录每次收到的消息，返回固定回答。"""

    _received: list[list[Any]] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "recording-test-model"

    def bind_tools(self, tools: Any, **kwargs: Any):
        return self

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._received.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="好的"))])


class _FakeKnowledgeBase:
    """只实现 list_texts 的假知识库，用于断言读取条件。"""

    def __init__(self, items: list[RetrievedChunk]) -> None:
        self.items = items
        self.calls: list[dict[str, Any]] = []

    def list_texts(self, **kwargs: Any) -> list[RetrievedChunk]:
        self.calls.append(kwargs)
        return list(self.items)


def _assistant_with(kb: _FakeKnowledgeBase) -> SecretaryAssistant:
    """绕过真实图构造，只测试依赖 knowledge_base 的偏好读取。"""

    assistant = SecretaryAssistant.__new__(SecretaryAssistant)
    assistant.knowledge_base = kb
    return assistant


def test_load_preferences_joins_preference_items() -> None:
    kb = _FakeKnowledgeBase(
        [
            RetrievedChunk(text="用户不希望任务汇报展示内部 id"),
            RetrievedChunk(text="用户喜欢先给结论再解释"),
        ]
    )

    text = _assistant_with(kb)._load_preferences()

    assert text == "- 用户不希望任务汇报展示内部 id\n- 用户喜欢先给结论再解释"
    assert kb.calls == [
        {
            "limit": 20,
            "where": {
                "$and": [
                    {"source": "auto_memory"},
                    {"kind": "preference"},
                ]
            },
        }
    ]


def test_load_preferences_returns_empty_without_items() -> None:
    kb = _FakeKnowledgeBase([])

    assert _assistant_with(kb)._load_preferences() == ""


def test_build_input_carries_preferences() -> None:
    data = SecretaryAssistant._build_input(
        "继续", [HumanMessage(content="上一轮")], "- 用户偏好简洁"
    )

    assert data["user_preferences"] == "- 用户偏好简洁"
    assert isinstance(data["messages"][-1], HumanMessage)


def _make_worker(model: BaseChatModel) -> WorkerAgent:
    return WorkerAgent(
        name="study_expert",
        description="学习专家",
        system_prompt="你是学习专家。",
        llm=model,
        tools=[],
    )


def test_worker_injects_preferences_into_system_prompt() -> None:
    model = _RecordingChatModel()
    worker = _make_worker(model)

    worker(
        {
            "messages": [HumanMessage(content="帮我看看这个")],
            "user_preferences": "- 用户不希望任务汇报展示内部 id",
        }
    )

    system = model._received[0][0]
    assert isinstance(system, SystemMessage)
    assert "用户偏好（务必遵守" in system.content
    assert "用户不希望任务汇报展示内部 id" in system.content


def test_worker_without_preferences_keeps_original_prompt() -> None:
    model = _RecordingChatModel()
    worker = _make_worker(model)

    worker({"messages": [HumanMessage(content="帮我看看这个")], "user_preferences": "  "})

    system = model._received[0][0]
    assert isinstance(system, SystemMessage)
    assert system.content == "你是学习专家。"
