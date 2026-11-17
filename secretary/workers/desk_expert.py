"""工作台专员：待办、日程与提醒。"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from ..prompts import DESK_EXPERT_PROMPT
from .base import WorkerAgent


class DeskExpert(WorkerAgent):
    """工具由编排层注入，实际读写 DeskStore 中的待办/日程。"""

    def __init__(self, llm: BaseChatModel, tools: Sequence[BaseTool]) -> None:
        super().__init__(
            name="desk_expert",
            description="处理待办、日程、提醒相关请求",
            system_prompt=DESK_EXPERT_PROMPT,
            llm=llm,
            tools=tools,
        )
