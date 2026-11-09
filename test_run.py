"""端到端演示入口。

真实模式（默认）：调用 DeepSeek，观察 Supervisor 如何把两个问题分别
路由给 study_expert 和 life_expert，并在回答后自动判断是否写入长期记忆。
默认使用临时知识库，不会污染正式 data/chroma；加 --live-kb 才使用正式库。

离线模式（--offline）：使用确定性的 DemoChatModel，不访问网络，仅验证
LangGraph 路由和工具调用骨架是否通畅（不写真实 Chroma）。

运行：
    python test_run.py
    python test_run.py --offline
    python test_run.py --live-kb
"""

from __future__ import annotations

import argparse
import sys
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from secretary.config import get_settings, require_api_key
from secretary.demo_model import DemoChatModel
from secretary.graph import (
    WORKER_NODES,
    SecretaryAssistant,
    extract_final_answer,
)
from secretary.llm import build_chat_model
from secretary.message_utils import message_text, truncate
from secretary.rag import ChromaKnowledgeBase

QUESTIONS = [
    "帮我讲讲高数里极限的定义，应该怎么理解？",
    "我想练腹肌，给我一个适合新手的健身计划。",
]


def _configure_stdout() -> None:
    """Windows 终端下尽量用 UTF-8 输出中文。"""

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _print_ai_message(message: AIMessage) -> None:
    tool_calls = message.tool_calls or []
    for call in tool_calls:
        print(f"    [工具调用] {call.get('name')}({call.get('args')})")

    text = message_text(message)
    if text:
        prefix = message.name or "ai"
        print(f"    [{prefix}] {truncate(text, 1200)}")


def run_case(assistant: SecretaryAssistant, question: str) -> tuple[str, set[str]]:
    """运行一个问题，打印每个节点的增量，返回最终回答和访问过的 Worker。"""

    print("\n" + "=" * 72)
    print(f"用户：{question}")
    print("-" * 72)

    visited_workers: set[str] = set()
    collected: list[BaseMessage] = []

    try:
        for update in assistant.stream(question):
            for node, payload in update.items():
                if node == "supervisor":
                    next_agent = payload.get("next_agent", "?")
                    reason = payload.get("route_reason", "")
                    print(f"  [supervisor] -> {next_agent}")
                    if reason:
                        print(f"    理由：{reason}")
                    if next_agent in WORKER_NODES:
                        visited_workers.add(next_agent)
                    continue

                if node == "memory_curator":
                    saved = payload.get("memories_saved", 0)
                    print(f"  [memory_curator] 写入/更新长期记忆：{saved} 条")
                    if payload.get("memory_error"):
                        print(f"    记忆写入失败：{payload['memory_error']}")
                    continue

                print(f"  [{node}]")
                for message in payload.get("messages", []):
                    collected.append(message)
                    if isinstance(message, ToolMessage):
                        print(f"    [工具结果] {truncate(message_text(message), 500)}")
                    elif isinstance(message, AIMessage):
                        _print_ai_message(message)
    except Exception as exc:
        print(f"\n[运行失败] {type(exc).__name__}: {exc}")
        raise

    answer = extract_final_answer({"messages": collected})
    print("-" * 72)
    print("最终回答：")
    print(answer)
    return answer, visited_workers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="个人秘书 RAG + Agent 端到端演示")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="不调用 DeepSeek，用确定性假模型验证路由和工具调用",
    )
    parser.add_argument(
        "--live-kb",
        action="store_true",
        help="使用正式 data/chroma；默认使用临时库，避免测试数据污染个人记忆",
    )
    args = parser.parse_args(argv)

    _configure_stdout()
    settings = get_settings()

    if args.offline:
        print("模式：离线演示（不调用 DeepSeek，仅验证图结构）")
        llm = DemoChatModel()
    else:
        print(
            f"模式：DeepSeek | model={settings.deepseek_model} | "
            f"base_url={settings.deepseek_base_url}"
        )
        try:
            require_api_key(settings)
        except RuntimeError as exc:
            print(f"\n{exc}")
            return 2
        llm = build_chat_model(settings)

    isolated_kb = args.offline or not args.live_kb
    print(
        "知识库：" + ("临时（不会写入正式数据）" if isolated_kb else "正式 data/chroma")
    )

    temp_context = (
        TemporaryDirectory(ignore_cleanup_errors=True, prefix="dsh-rag-demo-")
        if isolated_kb
        else nullcontext()
    )

    with temp_context as temp_dir:
        # 默认使用临时空知识库，避免演示问题产生的自动记忆污染正式数据
        knowledge_base = (
            ChromaKnowledgeBase(
                persist_dir=Path(temp_dir) / "chroma",
                collection_name="demo_knowledge",
            )
            if temp_dir
            else None
        )

        assistant = SecretaryAssistant(
            llm=llm,
            settings=settings,
            knowledge_base=knowledge_base,
            enable_auto_memory=not args.offline,
        )

        visited: set[str] = set()
        for question in QUESTIONS:
            _, case_workers = run_case(assistant, question)
            visited.update(case_workers)

        print("\n" + "=" * 72)
        missing = set(WORKER_NODES) - visited
        if missing:
            print(f"路由验证失败：没有访问到 Worker {sorted(missing)}")
            return 1

        print("路由验证通过：两个问题分别到达 study_expert 和 life_expert。")
        if args.offline:
            print(
                "提示：配置 DEEPSEEK_API_KEY 后去掉 --offline，即可验证真实模型链路。"
            )
        elif isolated_kb:
            print("提示：加 --live-kb 可使用正式知识库（会写入自动记忆）。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
