"""生活与技能专员：Kali、会计、健身。"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from ..prompts import LIFE_EXPERT_PROMPT
from .base import WorkerAgent


class LifeExpert(WorkerAgent):
    """工具由编排层注入，方便以后按领域挂载不同知识库。"""

    def __init__(self, llm: BaseChatModel, tools: Sequence[BaseTool]) -> None:
        super().__init__(
            name="life_expert",
            description="处理 Kali Linux、会计、健身相关问题",
            system_prompt=LIFE_EXPERT_PROMPT,
            llm=llm,
            tools=tools,
        )
