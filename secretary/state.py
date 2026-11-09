"""LangGraph 共享状态定义。

状态就是节点之间传递的“对话账本”：消息由 ``add_messages`` 自动合并，
路由结果单独存放，避免把控制字段混进消息内容。
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

# 与图节点名保持一致，FINISH 表示本轮任务结束
RouteName = Literal["study_expert", "life_expert", "FINISH"]


class SecretaryState(TypedDict, total=False):
    """Supervisor 和 Worker 共用的图状态。"""

    # Annotated + reducer：节点只返回新增消息，LangGraph 会追加而不是覆盖
    messages: Annotated[list[AnyMessage], add_messages]

    # Supervisor 的路由结果；Worker 不修改它
    next_agent: RouteName
    route_reason: str

    # 自动记忆节点的执行结果，供命令行展示
    memories_saved: int
    memory_ids: list[str]
    memory_error: str
