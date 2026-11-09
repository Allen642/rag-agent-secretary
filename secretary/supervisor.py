"""Supervisor（主管）Agent。

职责只有两件事：
  1. 读取当前对话，判断用户最新问题属于哪个领域；
  2. 输出下一个节点（某个 Worker 或 FINISH）。

DeepSeek 的思考模式（deepseek-flash）不支持强制 tool_choice，因此这里
不让模型调用“路由工具”，而是要求它直接输出一个 JSON 对象，再用
Pydantic 校验。这样既兼容思考模式，也保留了结构化路由的可验证性。
"""

from __future__ import annotations

import json
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field, ValidationError

from .json_utils import extract_json_object
from .message_utils import message_text, truncate
from .prompts import SUPERVISOR_SYSTEM_PROMPT
from .state import SecretaryState

ROUTE_FORMAT_INSTRUCTION = """\

请严格按下面格式输出一个 JSON 对象，不要输出 Markdown 代码块或额外解释：
{"next_agent": "study_expert 或 life_expert 或 FINISH", "reason": "一句话路由依据"}"""


class RouteDecision(BaseModel):
    """Supervisor 的结构化输出。"""

    next_agent: Literal["study_expert", "life_expert", "FINISH"] = Field(
        description="下一个要执行的节点；任务已完成时填 FINISH"
    )
    reason: str = Field(description="一句话说明路由依据")


def parse_route_decision(text: str) -> RouteDecision:
    """从模型文本中提取 JSON 并校验。"""

    try:
        data = extract_json_object(text)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"Supervisor 没有返回合法 JSON：{truncate(text.strip(), 500)}"
        ) from exc

    try:
        return RouteDecision.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"Supervisor 路由 JSON 字段不合法：{data!r}") from exc


def render_transcript(messages: list[AnyMessage], limit: int = 10) -> str:
    """把消息渲染成精简文本，避免把完整工具返回都塞给 Supervisor。"""

    lines: list[str] = []
    for message in messages[-limit:]:
        role = message.name or message.type
        text = message_text(message)

        tool_calls = getattr(message, "tool_calls", None) or []
        if tool_calls:
            call_text = "；".join(
                f"{call.get('name')}("
                f"{json.dumps(call.get('args', {}), ensure_ascii=False)})"
                for call in tool_calls
            )
            text = f"[请求调用工具] {call_text}"

        lines.append(f"[{role}] {truncate(text) or '（空消息）'}")

    return "\n".join(lines)


class Supervisor:
    """可被 LangGraph 调用的 Supervisor 节点。"""

    def __init__(self, llm: BaseChatModel) -> None:
        self._llm = llm

    def __call__(self, state: SecretaryState) -> dict:
        messages = list(state.get("messages", []))
        transcript = render_transcript(messages)

        # 把对话转成文本再交给路由模型：它只需要意图，不需要原始 ToolMessage 结构
        response = self._llm.invoke(
            [
                SystemMessage(
                    content=SUPERVISOR_SYSTEM_PROMPT + ROUTE_FORMAT_INSTRUCTION
                ),
                HumanMessage(
                    content=(f"下面是当前对话记录，请判断下一步节点。\n\n{transcript}")
                ),
            ]
        )
        decision = parse_route_decision(message_text(response))

        return {
            "next_agent": decision.next_agent,
            "route_reason": decision.reason,
        }
