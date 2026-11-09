"""验证“自动长期记忆”是否真的写入并能被检索。

运行：
    python test_memory.py

脚本使用临时 Chroma 目录，不会写入正式的 data/chroma。它会：
  1. 说一句包含个人习惯和目标的话（带“记住”触发词）；
  2. 检查 memory_curator 是否写入 Chroma；
  3. 再问一个依赖该记忆的问题，观察 Worker 是否检索到；
  4. 直接查询 Chroma，确认 source=auto_memory 的记录存在。
"""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from langchain_core.messages import ToolMessage

from secretary.config import get_settings, require_api_key
from secretary.graph import SecretaryAssistant, extract_final_answer
from secretary.llm import build_chat_model
from secretary.message_utils import message_text, truncate
from secretary.rag import ChromaKnowledgeBase

MEMORY_STATEMENT = "记住：我每周三和周五晚上健身，目标是增肌，目前体重 70 公斤。"
FOLLOW_UP = "根据你记住的我的健身习惯，帮我安排下周的训练。"


def _configure_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def run_demo(kb: ChromaKnowledgeBase) -> int:
    settings = get_settings()
    assistant = SecretaryAssistant(
        llm=build_chat_model(settings),
        settings=settings,
        knowledge_base=kb,
        enable_auto_memory=True,
    )

    print("=" * 72)
    print(f"用户：{MEMORY_STATEMENT}")
    result = assistant.invoke(MEMORY_STATEMENT)
    print(f"Supervisor 路由：{result.get('next_agent')}")
    saved = result.get("memories_saved", 0)
    print(f"自动记忆写入：{saved} 条")
    if result.get("memory_error"):
        print(f"记忆错误：{result['memory_error']}")
        return 1
    print("回答：")
    print(truncate(extract_final_answer(result), 700))

    print("\n" + "=" * 72)
    print(f"用户：{FOLLOW_UP}")
    result = assistant.invoke(FOLLOW_UP)
    print(f"Supervisor 路由：{result.get('next_agent')}")
    tool_results = [
        message_text(message)
        for message in result.get("messages", [])
        if isinstance(message, ToolMessage)
    ]
    print("本轮工具结果：")
    for text in tool_results[-3:]:
        print(truncate(text, 500))
    print("回答：")
    print(truncate(extract_final_answer(result), 900))

    print("\n" + "=" * 72)
    memories = kb.list_texts(limit=20, where={"source": "auto_memory"})
    print(f"Chroma 中的自动记忆：{len(memories)} 条")
    for memory in memories:
        print(f"- [{memory.metadata.get('topic')}] {memory.text} (id={memory.id})")

    hits = kb.search("健身习惯 增肌 训练频率", k=3)
    memory_hits = [
        chunk for chunk in hits if chunk.metadata.get("source") == "auto_memory"
    ]
    print(f"\n直接检索命中自动记忆：{len(memory_hits)} 条")

    if not memories or not memory_hits:
        print("\n验证失败：自动记忆没有正确写入或检索。")
        return 1

    print("\n验证通过：自动记忆已写入 Chroma，并能在后续检索中召回。")
    return 0


def main() -> int:
    _configure_stdout()
    settings = get_settings()
    try:
        require_api_key(settings)
    except RuntimeError as exc:
        print(exc)
        return 2

    with TemporaryDirectory(
        ignore_cleanup_errors=True, prefix="dsh-rag-memory-"
    ) as temp_dir:
        kb = ChromaKnowledgeBase(
            persist_dir=Path(temp_dir) / "chroma",
            collection_name="memory_demo",
        )
        print("本次使用临时知识库，不会写入正式 data/chroma。\n")
        return run_demo(kb)


if __name__ == "__main__":
    raise SystemExit(main())
