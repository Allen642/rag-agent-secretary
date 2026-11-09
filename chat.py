"""命令行聊天入口：连续对话 + 自动长期记忆。

运行：
    python chat.py              # 使用正式知识库 data/chroma
    python chat.py --temp-kb    # 使用临时知识库，退出即丢弃
    python chat.py --quiet      # 只显示最终回答，不显示路由/工具细节

对话中的特殊命令：
    /exit   退出
    /reset  清空当前对话上下文（不删除长期记忆）
    /mem    查看最近写入的长期记忆
    /help   显示帮助
"""

from __future__ import annotations

import argparse
import sys
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from secretary.config import get_settings, require_api_key
from secretary.graph import WORKER_NODES, SecretaryAssistant, extract_final_answer
from secretary.llm import build_chat_model
from secretary.message_utils import message_text, truncate
from secretary.rag import ChromaKnowledgeBase

MAX_HISTORY_MESSAGES = 20
EXIT_COMMANDS = {"/exit", "/quit", "exit", "quit", "退出", "再见"}

HELP_TEXT = """\
可用命令：
  /exit   退出对话
  /reset  清空当前上下文（长期记忆保留）
  /mem    查看最近写入的长期记忆
  /help   显示帮助

直接输入问题即可；想说“记住……”时它会自动写入长期记忆。
"""


def _configure_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _trim_history(history: list[BaseMessage]) -> list[BaseMessage]:
    """只保留最近若干条消息，避免上下文无限增长导致费用和延迟上升。"""

    return history[-MAX_HISTORY_MESSAGES:]


def _setup_console() -> None:
    """尽量启用 Windows 终端的 ANSI 颜色；失败则静默跳过。"""

    try:
        from colorama import just_fix_windows_console

        just_fix_windows_console()
    except ImportError:
        # 颜色只是锦上添花，没有 colorama 也不影响聊天
        return


class _Painter:
    """简单的 ANSI 着色，避免为聊天界面引入额外依赖。"""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)


def run_turn(
    assistant: SecretaryAssistant,
    question: str,
    history: list[BaseMessage],
    painter: _Painter,
    *,
    verbose: bool,
) -> list[BaseMessage]:
    """执行一轮对话并打印过程，返回本轮新增的消息用于更新上下文。"""

    new_messages: list[BaseMessage] = []

    for update in assistant.stream(question, history=history):
        for node, payload in update.items():
            if node == "supervisor":
                next_agent = payload.get("next_agent", "?")
                reason = payload.get("route_reason", "")
                if verbose and next_agent in WORKER_NODES:
                    print(painter.dim(f"  [路由] {next_agent}：{reason}"))
                continue

            if node == "memory_curator":
                saved = payload.get("memories_saved", 0)
                if saved:
                    print(painter.green(f"  [记忆] 已写入/更新 {saved} 条长期记忆"))
                if payload.get("memory_error"):
                    print(
                        painter.yellow(f"  [记忆] 写入失败：{payload['memory_error']}")
                    )
                continue

            for message in payload.get("messages", []):
                if isinstance(message, ToolMessage):
                    new_messages.append(message)
                    if verbose:
                        print(
                            painter.dim(
                                f"  [检索] {truncate(message_text(message), 200)}"
                            )
                        )
                elif isinstance(message, AIMessage):
                    new_messages.append(message)
                    if message.tool_calls and verbose:
                        for call in message.tool_calls:
                            print(
                                painter.dim(
                                    f"  [工具调用] {call.get('name')}"
                                    f"({call.get('args')})"
                                )
                            )

    return new_messages


def chat_loop(assistant: SecretaryAssistant, *, verbose: bool, color: bool) -> int:
    painter = _Painter(color)
    history: list[BaseMessage] = []

    print(painter.cyan("个人秘书已启动。输入 /help 查看命令，/exit 退出。"))
    print(painter.dim(f"长期记忆位置：{assistant.knowledge_base.persist_dir}"))
    print(painter.dim(f"当前记忆条数：{assistant.knowledge_base.count()}"))

    while True:
        try:
            raw = input(painter.cyan("\n你 > "))
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return 0

        question = raw.strip()
        if not question:
            continue

        if question.lower() in EXIT_COMMANDS:
            print("再见。")
            return 0

        if question in ("/help", "/h", "help"):
            print(HELP_TEXT)
            continue

        if question == "/reset":
            history.clear()
            print(painter.dim("已清空当前对话上下文；长期记忆仍然保留。"))
            continue

        if question == "/mem":
            _print_memories(assistant, painter)
            continue

        try:
            new_messages = run_turn(
                assistant, question, history, painter, verbose=verbose
            )
        except KeyboardInterrupt:
            print("\n已中断本轮回答。")
            continue
        except Exception as exc:  # noqa: BLE001 - 聊天界面不能因为单轮失败退出
            print(f"\n[本轮失败] {type(exc).__name__}: {exc}")
            continue

        answer = extract_final_answer({"messages": new_messages})
        print()
        print(answer)

        # 自动记忆节点已经读取过历史，更新上下文时要包含本轮用户问题
        history.append(HumanMessage(content=question))
        for message in new_messages:
            if isinstance(message, AIMessage) and message.name in WORKER_NODES:
                history.append(
                    AIMessage(content=message_text(message), name=message.name)
                )
        history[:] = _trim_history(history)


def _print_memories(assistant: SecretaryAssistant, painter: _Painter) -> None:
    memories = assistant.knowledge_base.list_texts(
        limit=20, where={"source": "auto_memory"}
    )
    if not memories:
        print(painter.dim("还没有自动记忆。"))
        return

    print(painter.cyan(f"最近 {len(memories)} 条自动记忆："))
    for memory in memories:
        topic = memory.metadata.get("topic", "个人")
        print(f"  - [{topic}] {memory.text}")
        print(painter.dim(f"    id={memory.id}"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="个人秘书命令行聊天")
    parser.add_argument("--temp-kb", action="store_true", help="使用临时知识库")
    parser.add_argument("--quiet", action="store_true", help="隐藏路由/检索细节")
    parser.add_argument("--no-color", action="store_true", help="关闭颜色")
    args = parser.parse_args(argv)

    _configure_stdout()
    _setup_console()

    settings = get_settings()
    try:
        require_api_key(settings)
    except RuntimeError as exc:
        print(exc)
        return 2

    temp_context = (
        TemporaryDirectory(ignore_cleanup_errors=True, prefix="dsh-rag-chat-")
        if args.temp_kb
        else nullcontext()
    )

    with temp_context as temp_dir:
        knowledge_base = (
            ChromaKnowledgeBase(
                persist_dir=Path(temp_dir) / "chroma",
                collection_name="chat_temp",
            )
            if temp_dir
            else ChromaKnowledgeBase()
        )
        assistant = SecretaryAssistant(
            llm=build_chat_model(settings),
            settings=settings,
            knowledge_base=knowledge_base,
            enable_auto_memory=True,
        )
        if args.temp_kb:
            print("注意：本次使用临时知识库，退出后所有记忆和资料都会丢弃。")

        return chat_loop(
            assistant,
            verbose=not args.quiet,
            color=not args.no_color,
        )


if __name__ == "__main__":
    raise SystemExit(main())
