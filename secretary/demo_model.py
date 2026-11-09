"""仅用于离线演示的确定性“假模型”。

它不调用任何网络服务，目的是在没有 DeepSeek API Key 的情况下验证：
  * LangGraph 的 Supervisor -> Worker -> Supervisor -> FINISH 路由；
  * Worker 的 bind_tools / ToolMessage 工具调用循环；
  * test_run.py 的输出展示。

它不是生产模型，也不参与默认运行路径。真实运行时请使用
``secretary.llm.build_chat_model`` 创建的 DeepSeek ChatOpenAI。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult

STUDY_KEYWORDS = (
    "高数",
    "数学",
    "极限",
    "导数",
    "积分",
    "c语言",
    "c 语言",
    "指针",
    "数组",
    "pte",
    "英语",
    "考试",
    "编程",
    "代码",
)
LIFE_KEYWORDS = (
    "健身",
    "腹肌",
    "肌肉",
    "训练",
    "减脂",
    "饮食",
    "kali",
    "会计",
    "记账",
    "财务",
    "报表",
)


def _tool_names(tools: Sequence[Any]) -> set[str]:
    names: set[str] = set()
    for item in tools:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict):
            function = item.get("function", {})
            if isinstance(function, dict) and function.get("name"):
                names.add(str(function["name"]))
            elif item.get("name"):
                names.add(str(item["name"]))
        else:
            names.add(getattr(item, "name", getattr(item, "__name__", "")))
    return {name for name in names if name}


def _latest_human_text(messages: Sequence[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content)
    return ""


def _transcript_question(text: str) -> str:
    """从 Supervisor 的文本化对话中提取用户最新问题。"""

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("[human]"):
            return line.removeprefix("[human]").strip()
    return text.strip()


class DemoChatModel(BaseChatModel):
    """根据绑定工具类型自动扮演 Supervisor 或 Worker。"""

    @property
    def _llm_type(self) -> str:
        return "demo-offline-chat-model"

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ):
        # 把工具信息绑定到 Runnable 上，_generate 中通过 kwargs 读取
        return self.bind(tools=tools, tool_choice=tool_choice, **kwargs)

    def _route_decision(self, messages: Sequence[BaseMessage]) -> AIMessage:
        transcript = str(messages[-1].content) if messages else ""
        question = _transcript_question(transcript)
        lines = [line.strip() for line in transcript.splitlines() if line.strip()]

        # 注意：Worker 的最终回答可能有多行，不能只看最后一行，
        # 要找到最后一个“[角色]”标记再判断它是不是 Worker。
        role_lines = [
            line
            for line in lines
            if line.startswith(
                (
                    "[human]",
                    "[ai]",
                    "[tool]",
                    "[system]",
                    "[study_expert]",
                    "[life_expert]",
                )
            )
        ]

        # Worker 已经回答：让 Supervisor 结束本轮
        if role_lines and (
            role_lines[-1].startswith("[study_expert]")
            or role_lines[-1].startswith("[life_expert]")
        ):
            next_agent = "FINISH"
            reason = "Worker 已给出回答，本轮任务完成"
        else:
            lowered = question.lower()
            study_hit = any(keyword in lowered for keyword in STUDY_KEYWORDS)
            life_hit = any(keyword in lowered for keyword in LIFE_KEYWORDS)
            if life_hit and not study_hit:
                next_agent = "life_expert"
                reason = "问题涉及健身/生活技能，交给 life_expert"
            else:
                next_agent = "study_expert"
                reason = "问题涉及学业内容，交给 study_expert"

        return AIMessage(
            content=json.dumps(
                {"next_agent": next_agent, "reason": reason},
                ensure_ascii=False,
            )
        )

    def _worker_reply(self, messages: Sequence[BaseMessage]) -> AIMessage:
        question = _latest_human_text(messages)

        # 只看“当前这轮问题”之后是否有工具结果，避免多轮对话时误判为已检索
        last_human_index = 0
        for index, message in enumerate(messages):
            if isinstance(message, HumanMessage):
                last_human_index = index
        tool_messages = [
            message
            for message in messages[last_human_index + 1 :]
            if isinstance(message, ToolMessage)
        ]

        if tool_messages:
            tool_result = str(tool_messages[-1].content)
            content = (
                "【离线演示】我已经调用了 search_knowledge_base 工具。\n"
                f"你的问题是：{question}\n\n"
                f"工具返回：\n{tool_result}\n\n"
                "以上内容用于证明 Supervisor 路由和 Worker 工具调用链路已经打通；"
                "配置 DEEPSEEK_API_KEY 后，同样的流程会由 DeepSeek 生成真实回答。"
            )
            return AIMessage(content=content)

        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "search_knowledge_base",
                    "args": {"query": question},
                    "id": f"call-search-{uuid4().hex[:8]}",
                    "type": "tool_call",
                }
            ],
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        tools = kwargs.get("tools") or []
        names = _tool_names(tools)
        last_text = str(messages[-1].content) if messages else ""

        # Supervisor 现在不绑定工具，而是要求模型直接输出 JSON
        if "下面是当前对话记录" in last_text:
            message = self._route_decision(messages)
        elif "search_knowledge_base" in names:
            message = self._worker_reply(messages)
        else:
            # Worker 达到工具上限后的兜底调用（没有绑定工具）
            message = AIMessage(content="【离线演示】已达到工具调用上限，直接结束。")

        return ChatResult(generations=[ChatGeneration(message=message)])
