"""真实知识库检索工具。

Worker 通过这个 Tool 读取本地 ChromaDB。知识库由两部分组成：
  * source=auto_memory：每轮对话后自动提取的长期记忆；
  * 其他 source：手动写入的领域资料/笔记。

检索是只读的；写入由 MemoryCurator 和管理 CLI 负责，避免 Worker
在回答问题时无意中改坏知识库。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from ..rag import ChromaKnowledgeBase


def make_search_tool(
    knowledge_base: ChromaKnowledgeBase,
    *,
    k: int = 4,
    max_distance: float = 0.65,
) -> BaseTool:
    """创建一个绑定到指定知识库的检索 Tool。"""

    @tool("search_knowledge_base")
    def search_knowledge_base(query: str) -> str:
        """在个人知识库和长期记忆中检索与问题相关的资料。

        Args:
            query: 从用户问题中提炼出的检索关键词。
        """

        chunks = knowledge_base.search(query, k=k)
        relevant = [
            chunk
            for chunk in chunks
            if chunk.distance is None or chunk.distance <= max_distance
        ]
        if not relevant:
            return f"知识库中暂未检索到与“{query}”相关的内容。"

        lines: list[str] = []
        for chunk in relevant:
            source = chunk.metadata.get("source", "knowledge")
            topic = chunk.metadata.get("topic", "知识库")
            label = "长期记忆" if source == "auto_memory" else f"知识库/{topic}"
            lines.append(f"[{label}] {chunk.text}")

        return "\n\n".join(lines)

    return search_knowledge_base
