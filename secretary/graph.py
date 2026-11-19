"""LangGraph 编排：Supervisor + 三个 Worker + 自动记忆。

图结构（Supervisor 模式）：

    START -> supervisor --(study_expert)--> study_expert --+
                     |                                     |
                     +----(life_expert)--> life_expert -----+
                     |                                     |
                     +----(desk_expert)--> desk_expert -----+
                     |                                     |
                     +----(FINISH)--> memory_curator -> END
                                      ^
                                      |
                              Worker 完成后回到 supervisor

Worker 回到 Supervisor 是为了让主管确认任务完成（选择 FINISH），然后
由 memory_curator 在每轮结束时自动判断是否写入长期记忆。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.graph import END, START, StateGraph

from .config import Settings, get_settings
from .desk import DeskStore
from .llm import build_chat_model
from .memory import MemoryCurator
from .message_utils import message_text
from .rag import ChromaKnowledgeBase
from .state import SecretaryState
from .supervisor import Supervisor
from .tools import make_desk_tools, make_search_tool
from .workers import DeskExpert, LifeExpert, StudyExpert

WORKER_NODES = ("study_expert", "life_expert", "desk_expert")
DEFAULT_RECURSION_LIMIT = 20
# 短期对话上下文最多保留的消息条数（用户消息 + Worker 最终回答）
HISTORY_LIMIT = 20


def _route_from_supervisor(state: SecretaryState) -> str:
    """把 Supervisor 的决策映射成 LangGraph 的条件边。

    增加兜底：即使模型输出了非法节点名，也结束本轮而不是让图报错。
    """

    next_agent = state.get("next_agent", "FINISH")
    return next_agent if next_agent in WORKER_NODES else "FINISH"


def build_secretary_graph(
    llm: BaseChatModel,
    *,
    knowledge_base: ChromaKnowledgeBase | None = None,
    desk_store: DeskStore | None = None,
    enable_auto_memory: bool = True,
):
    """组装并编译 Secretary 图。

    参数 ``llm`` 由外部注入，既可以是 DeepSeek，也可以是测试用模型，
    这样图结构本身可以脱离网络独立测试。``enable_auto_memory=False``
    可用于离线演示或单元测试，避免产生真实记忆写入。
    ``desk_store`` 不传时使用默认的 ``data/runtime/desk.db``；测试和演示
    应注入临时库，避免污染真实工作台数据。
    """

    knowledge_base = knowledge_base or ChromaKnowledgeBase()
    desk_store = desk_store or DeskStore()
    search_tool = make_search_tool(knowledge_base)

    supervisor = Supervisor(llm)
    study_expert = StudyExpert(llm, [search_tool])
    life_expert = LifeExpert(llm, [search_tool])
    desk_expert = DeskExpert(llm, make_desk_tools(desk_store))

    builder = StateGraph(SecretaryState)
    builder.add_node("supervisor", supervisor)
    builder.add_node("study_expert", study_expert)
    builder.add_node("life_expert", life_expert)
    builder.add_node("desk_expert", desk_expert)

    finish_target = END
    if enable_auto_memory:
        builder.add_node("memory_curator", MemoryCurator(llm, knowledge_base))
        builder.add_edge("memory_curator", END)
        finish_target = "memory_curator"

    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges(
        "supervisor",
        _route_from_supervisor,
        {
            "study_expert": "study_expert",
            "life_expert": "life_expert",
            "desk_expert": "desk_expert",
            "FINISH": finish_target,
        },
    )

    # 三个 Worker 都回到主管，由主管判断是否 FINISH
    builder.add_edge("study_expert", "supervisor")
    builder.add_edge("life_expert", "supervisor")
    builder.add_edge("desk_expert", "supervisor")

    return builder.compile()


class SecretaryAssistant:
    """面向调用方的门面：隐藏图、模型和消息细节。"""

    def __init__(
        self,
        *,
        llm: BaseChatModel | None = None,
        settings: Settings | None = None,
        knowledge_base: ChromaKnowledgeBase | None = None,
        desk_store: DeskStore | None = None,
        enable_auto_memory: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self.llm = llm or build_chat_model(self.settings)
        self.knowledge_base = knowledge_base or ChromaKnowledgeBase()
        self.desk_store = desk_store or DeskStore()
        self._enable_auto_memory = enable_auto_memory
        self._model_lock = threading.Lock()
        self.graph = build_secretary_graph(
            self.llm,
            knowledge_base=self.knowledge_base,
            desk_store=self.desk_store,
            enable_auto_memory=self._enable_auto_memory,
        )

    def switch_model(self, provider: str | None = None, model: str | None = None) -> str:
        """切换 provider/model 并重建图；复用 knowledge_base/desk_store，不重载 embedding。

        provider 只接受 deepseek / zhipu；model 可选，指定则覆盖该 provider 的模型名。
        切换失败（如 zhipu key 缺失）抛 RuntimeError，由调用方转成可读提示。
        """

        with self._model_lock:
            provider = (provider or self.settings.model_provider).strip().lower()
            if provider not in ("deepseek", "zhipu"):
                raise ValueError(f"不支持的 provider：{provider}")
            settings = replace(self.settings, model_provider=provider)
            if model:
                if provider == "zhipu":
                    settings = replace(settings, zhipu_model=model)
                else:
                    settings = replace(settings, deepseek_model=model)
            new_llm = build_chat_model(settings)  # 可能抛 RuntimeError（key 缺失）
            self.settings = settings
            self.llm = new_llm
            self.graph = build_secretary_graph(
                self.llm,
                knowledge_base=self.knowledge_base,
                desk_store=self.desk_store,
                enable_auto_memory=self._enable_auto_memory,
            )
            current = settings.zhipu_model if provider == "zhipu" else settings.deepseek_model
            return f"已切换到 {provider}/{current}"

    def stream(
        self,
        question: str,
        *,
        history: list[AnyMessage] | None = None,
        recursion_limit: int = DEFAULT_RECURSION_LIMIT,
    ) -> Iterator[dict[str, Any]]:
        """流式返回每个节点的增量，便于命令行展示路由过程。

        ``history`` 用于连续对话：传入之前轮次的 user/assistant 消息即可。
        """

        return self.graph.stream(
            self._build_input(question, history),
            stream_mode="updates",
            config={"recursion_limit": recursion_limit},
        )

    def stream_events(
        self,
        question: str,
        *,
        history: list[AnyMessage] | None = None,
        recursion_limit: int = DEFAULT_RECURSION_LIMIT,
    ) -> Iterator[tuple[str, Any]]:
        """同时返回节点更新和 LLM token，供 Web UI 做实时展示。

        事件形式为 ``(mode, data)``：
          * mode="updates"：节点执行完成后的状态增量；
          * mode="messages"：模型 token 与元数据，可用 metadata["langgraph_node"]
            过滤出某个 Worker 的最终回答，避免把 Supervisor 的 JSON 也打到界面上。
        """

        return self.graph.stream(
            self._build_input(question, history),
            stream_mode=["updates", "messages"],
            config={"recursion_limit": recursion_limit},
        )

    def invoke(
        self,
        question: str,
        *,
        history: list[AnyMessage] | None = None,
        recursion_limit: int = DEFAULT_RECURSION_LIMIT,
    ) -> dict[str, Any]:
        """执行一轮完整对话并返回最终图状态。"""

        return self.graph.invoke(
            self._build_input(question, history),
            config={"recursion_limit": recursion_limit},
        )

    @staticmethod
    def _build_input(question: str, history: list[AnyMessage] | None) -> dict[str, Any]:
        """组装本轮输入：历史消息 + 当前问题。"""

        messages: list[AnyMessage] = [*(history or []), HumanMessage(content=question)]
        return {"messages": messages}

    def ask(
        self,
        question: str,
        *,
        recursion_limit: int = DEFAULT_RECURSION_LIMIT,
    ) -> str:
        """一次性执行并返回最终答案。"""

        return extract_final_answer(
            self.invoke(question, recursion_limit=recursion_limit)
        )

    def ask_turn(
        self,
        question: str,
        *,
        history: list[AnyMessage] | None = None,
        recursion_limit: int = DEFAULT_RECURSION_LIMIT,
    ) -> tuple[str, list[AnyMessage]]:
        """执行一轮对话，同时返回回答和可用于下一轮的短期历史。

        与 :meth:`ask` 的区别是：这里从最终图状态中过滤出干净的
        “用户消息 + Worker 最终回答”，去掉工具调用、ToolMessage 和
        Supervisor 的中间结果，调用方可以直接把返回值作为下一轮的
        ``history`` 传入，不必关心 LangGraph 的内部消息结构。
        """

        state = self.invoke(
            question,
            history=history,
            recursion_limit=recursion_limit,
        )
        return extract_final_answer(state), self._conversation_history(state)

    @staticmethod
    def _conversation_history(
        state: SecretaryState | dict[str, Any],
        *,
        limit: int = HISTORY_LIMIT,
    ) -> list[AnyMessage]:
        """从最终状态中提取适合复用的自然对话历史。

        只保留：
          * 用户消息（HumanMessage）；
          * 不带 tool_calls 的 Worker 最终回答（AIMessage.name 是 Worker 名）。

        带 tool_calls 的 AI 消息、ToolMessage、Supervisor 的路由 JSON 等
        中间产物都会被过滤掉，避免下一轮把不完整的工具调用配对带进图。
        """

        kept: list[AnyMessage] = []
        for message in state.get("messages", []):
            if isinstance(message, HumanMessage):
                if message_text(message):
                    kept.append(message)
                continue

            if (
                isinstance(message, AIMessage)
                and message.name in WORKER_NODES
                and not getattr(message, "tool_calls", None)
                and message_text(message)
            ):
                kept.append(message)

        return kept[-limit:]


def extract_final_answer(state: SecretaryState | dict[str, Any]) -> str:
    """从图状态中取最后一条 Worker 的最终回答。"""

    messages = list(state.get("messages", []))
    for message in reversed(messages):
        if isinstance(message, AIMessage) and message.name in WORKER_NODES:
            return message_text(message)
    return "（没有得到 Worker 的最终回答）"
