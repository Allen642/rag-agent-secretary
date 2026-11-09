"""学业专员：高数、C 语言、PTE。"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from ..prompts import STUDY_EXPERT_PROMPT
from .base import WorkerAgent


class StudyExpert(WorkerAgent):
    """工具由编排层注入，方便以后按领域挂载不同知识库。"""

    def __init__(self, llm: BaseChatModel, tools: Sequence[BaseTool]) -> None:
        super().__init__(
            name="study_expert",
            description="处理高等数学、C 语言、PTE 英语考试相关问题",
            system_prompt=STUDY_EXPERT_PROMPT,
            llm=llm,
            tools=tools,
        )
