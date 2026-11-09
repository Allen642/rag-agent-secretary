"""Worker Agent 的通用实现。

一个 Worker 本质上是一个“带工具的小型 ReAct 循环”：
  1. 让模型决定是否调用工具；
  2. 如果调用了，执行工具并把结果作为 ToolMessage 回填；
  3. 再次询问模型，直到模型给出最终回答或达到工具调用上限。

把循环放在基类里，新增 Worker 时只需要提供名称、领域提示词和工具列表。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool

from ..state import SecretaryState

logger = logging.getLogger(__name__)


class WorkerAgent:
    """可被 LangGraph 当作节点调用的 Worker。"""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        system_prompt: str,
        llm: BaseChatModel,
        tools: Sequence[BaseTool],
        max_tool_rounds: int = 3,
    ) -> None:
        self.name = name
        self.description = description
        self.system_prompt = system_prompt
        self._llm = llm
        # bind_tools 只影响这个 Worker，不会污染 Supervisor 的模型实例
        self._tool_llm = llm.bind_tools(list(tools))
        self._tools_by_name = {tool.name: tool for tool in tools}
        self.max_tool_rounds = max_tool_rounds

    def _execute_tool_call(self, tool_call: dict) -> str:
        """执行单个工具调用；工具异常转成文本回填，避免整张图崩掉。"""

        tool_name = tool_call.get("name", "")
        tool = self._tools_by_name.get(tool_name)
        if tool is None:
            return f"工具调用失败：未知工具 {tool_name!r}"

        try:
            result = tool.invoke(tool_call.get("args") or {})
        except Exception as exc:
            logger.exception("Worker %s 调用工具 %s 失败", self.name, tool_name)
            return f"工具调用失败：{type(exc).__name__}: {exc}"

        return str(result)

    def __call__(self, state: SecretaryState) -> dict:
        """LangGraph 节点入口：返回本节点新增的消息。"""

        conversation = list(state.get("messages", []))
        messages: list[AnyMessage] = [
            SystemMessage(content=self.system_prompt),
            *conversation,
        ]
        new_messages: list[AnyMessage] = []

        response = self._tool_llm.invoke(messages)

        for _ in range(self.max_tool_rounds):
            if not getattr(response, "tool_calls", None):
                break

            # 带 tool_calls 的 AI 消息必须先进入上下文，ToolMessage 才能与它配对
            messages.append(response)
            new_messages.append(response)

            for tool_call in response.tool_calls:
                result = self._execute_tool_call(tool_call)
                tool_message = ToolMessage(
                    content=result,
                    tool_call_id=tool_call["id"],
                    name=tool_call.get("name", ""),
                )
                messages.append(tool_message)
                new_messages.append(tool_message)

            response = self._tool_llm.invoke(messages)

        if getattr(response, "tool_calls", None):
            # 达到上限仍在请求工具：用不带工具的模型强制收尾，防止无限循环
            logger.warning("Worker %s 达到工具调用上限，强制生成最终回答", self.name)
            messages.append(response)
            new_messages.append(response)
            response = self._llm.invoke(
                [
                    *messages,
                    HumanMessage(
                        content=(
                            "工具调用次数已经达到上限。请直接基于目前得到的信息"
                            "给出最终回答，不要再调用任何工具。"
                        )
                    ),
                ]
            )

        # 给最终 AI 消息打上 Worker 名字，Supervisor 据此判断“已经有 Worker 回答过”
        response = response.model_copy(update={"name": self.name})
        new_messages.append(response)
        return {"messages": new_messages}
