"""个人秘书 Streamlit Web UI。

启动：
    streamlit run app.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import streamlit as st
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from secretary.config import get_settings, require_api_key
from secretary.graph import WORKER_NODES, SecretaryAssistant
from secretary.llm import build_chat_model
from secretary.message_utils import content_to_text, message_text

MAX_HISTORY_MESSAGES = 20
MAX_MEMORY_PREVIEW = 10

st.set_page_config(
    page_title="个人秘书",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_resource(show_spinner="正在初始化个人秘书……")
def get_assistant() -> SecretaryAssistant:
    """创建并缓存带流式输出的 DeepSeek 助手。"""

    settings = get_settings()
    require_api_key(settings)
    llm = build_chat_model(settings, streaming=True)
    return SecretaryAssistant(
        llm=llm,
        settings=settings,
        enable_auto_memory=True,
    )


def _init_state() -> None:
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "history" not in st.session_state:
        st.session_state.history = []


def _trim_history() -> None:
    st.session_state.history = st.session_state.history[-MAX_HISTORY_MESSAGES:]


def _tool_results(messages: list[BaseMessage]) -> list[dict[str, str]]:
    """从 Worker 节点新增消息中提取检索 query 和工具返回原文。"""

    calls: dict[str, str] = {}
    results: list[dict[str, str]] = []
    for message in messages:
        if isinstance(message, AIMessage) and message.tool_calls:
            for call in message.tool_calls:
                calls[call.get("id", "")] = str(
                    (call.get("args") or {}).get("query", "")
                )
        elif isinstance(message, ToolMessage):
            results.append(
                {
                    "query": calls.get(message.tool_call_id, ""),
                    "text": message_text(message),
                }
            )
    return results


def _render_trace(trace: dict[str, Any]) -> None:
    """展示主管路由和 Worker 检索来源。"""

    if not trace:
        return

    routes = trace.get("routes") or []
    tool_results = trace.get("tool_results") or []
    memories_saved = trace.get("memories_saved", 0)
    memory_error = trace.get("memory_error")

    if not routes and not tool_results and not memories_saved and not memory_error:
        return

    with st.expander("🧭 思考过程与检索来源", expanded=False):
        if routes:
            st.markdown("**主管路由**")
            for step in routes:
                target = step.get("next_agent", "?")
                reason = step.get("reason", "")
                st.markdown(f"- `{target}`：{reason}")

        if tool_results:
            st.markdown("**检索来源**")
            for index, item in enumerate(tool_results, start=1):
                query = item.get("query") or "（未记录查询）"
                st.caption(f"检索 {index} · query = {query}")
                st.text(item.get("text", ""))

        if memories_saved:
            st.success(f"本轮写入/更新长期记忆：{memories_saved} 条")
        if memory_error:
            st.warning(f"长期记忆写入失败：{memory_error}")


def _render_message(message: dict[str, Any]) -> None:
    role = message.get("role", "assistant")
    avatar = "🧑" if role == "user" else "🤖"
    with st.chat_message(role, avatar=avatar):
        st.markdown(message.get("content", ""))
        trace = message.get("trace")
        if trace:
            _render_trace(trace)


def _memory_items(assistant: SecretaryAssistant) -> list[Any]:
    items = assistant.knowledge_base.list_texts(
        limit=50, where={"source": "auto_memory"}
    )
    items.sort(
        key=lambda item: item.metadata.get("updated_at", ""),
        reverse=True,
    )
    return items[:MAX_MEMORY_PREVIEW]


def _render_sidebar(assistant: SecretaryAssistant) -> None:
    settings = get_settings()
    with st.sidebar:
        st.header("🤖 个人秘书")
        st.caption(f"模型：`{settings.deepseek_model}` · CPU Embedding")
        st.caption(f"知识库：`{assistant.knowledge_base.persist_dir}`")

        col1, col2 = st.columns(2)
        with col1:
            st.metric("知识片段", assistant.knowledge_base.count())
        with col2:
            st.metric(
                "自动记忆",
                assistant.knowledge_base.count(where={"source": "auto_memory"}),
            )

        st.divider()

        if st.button("🧹 清空对话", use_container_width=True):
            # 只清空当前会话上下文，长期记忆和知识库保持不变
            st.session_state.messages = []
            st.session_state.history = []
            st.rerun()

        with st.expander("🧠 查看长期记忆", expanded=False):
            if st.button("刷新记忆列表", key="refresh_memories"):
                st.rerun()

            memories = _memory_items(assistant)
            if not memories:
                st.info("还没有长期记忆。和秘书聊天，或直接说“记住：……”。")
            else:
                st.caption(f"最近 {len(memories)} 条")
                for item in memories:
                    topic = item.metadata.get("topic", "个人")
                    updated = item.metadata.get("updated_at", "")
                    st.markdown(f"**{topic}**")
                    st.markdown(item.text)
                    st.caption(f"id = {item.id} · {updated}")

        st.divider()
        st.caption(
            "提示：清空对话不会删除长期记忆。"
            "需要删除记忆时，使用 scripts/kb.py delete <id>。"
        )


def _stream_answer(
    assistant: SecretaryAssistant,
    prompt: str,
    history: list[BaseMessage],
    status: Any,
    answer_placeholder: Any,
) -> tuple[str, dict[str, Any]]:
    """执行一轮图，实时更新状态和答案，最后返回答案与 trace。"""

    answer_parts: list[str] = []
    route_steps: list[dict[str, str]] = []
    tool_results: list[dict[str, str]] = []
    memories_saved = 0
    memory_error = ""
    fallback_answer = ""
    last_worker = ""

    for mode, data in assistant.stream_events(prompt, history=history):
        if mode == "messages":
            chunk, metadata = data
            node = metadata.get("langgraph_node", "")
            if node not in WORKER_NODES:
                # 过滤 Supervisor 的 JSON 和 memory_curator 的 JSON，
                # 只把 Worker 的最终回答拼到界面上
                continue
            text = content_to_text(getattr(chunk, "content", ""))
            if text:
                answer_parts.append(text)
                answer_placeholder.markdown("".join(answer_parts) + "▌")
            continue

        if mode != "updates":
            continue

        for node, payload in (data or {}).items():
            if node == "supervisor":
                next_agent = payload.get("next_agent", "?")
                reason = payload.get("route_reason", "")
                route_steps.append(
                    {"next_agent": str(next_agent), "reason": str(reason)}
                )
                if next_agent == "FINISH":
                    status.update(
                        label="🤖 主管正在整理最终结果……",
                        state="running",
                        expanded=True,
                    )
                else:
                    status.update(
                        label=f"🤖 主管正在思考…… 路由至 {next_agent}",
                        state="running",
                        expanded=True,
                    )
                continue

            if node == "memory_curator":
                memories_saved += int(payload.get("memories_saved", 0) or 0)
                if payload.get("memory_error"):
                    memory_error = str(payload["memory_error"])
                if memories_saved or memory_error:
                    status.update(
                        label="🧠 正在更新长期记忆……",
                        state="running",
                        expanded=True,
                    )
                continue

            if node in WORKER_NODES:
                last_worker = node
                status.update(
                    label=f"🔎 {node} 正在检索知识库并组织回答……",
                    state="running",
                    expanded=True,
                )
                messages = list(payload.get("messages", []) or [])
                tool_results.extend(_tool_results(messages))
                for message in messages:
                    if (
                        isinstance(message, AIMessage)
                        and message.name in WORKER_NODES
                        and not message.tool_calls
                    ):
                        fallback_answer = message_text(message)

    answer = "".join(answer_parts).strip()
    if not answer:
        # 某些模型或代理不支持 token 回调时，回退到节点完成后的完整消息
        answer = fallback_answer or "（没有生成回答）"
    answer_placeholder.markdown(answer)

    trace = {
        "routes": route_steps,
        "tool_results": tool_results,
        "memories_saved": memories_saved,
        "memory_error": memory_error,
        "worker": last_worker,
    }
    return answer, trace


def main() -> None:
    _init_state()

    st.title("🤖 个人秘书")
    st.caption("Supervisor 路由 · 本地 Chroma 知识库 · 自动长期记忆 · 流式回答")

    try:
        assistant = get_assistant()
    except Exception as exc:  # noqa: BLE001 - 页面需要展示可读错误
        st.error(f"初始化失败：{type(exc).__name__}: {exc}")
        st.info(
            "请确认 `.env` 中已经填写 DEEPSEEK_API_KEY，"
            "并已执行 `pip install -r requirements.txt`。"
        )
        st.stop()

    _render_sidebar(assistant)

    if not st.session_state.messages:
        st.info(
            "直接输入问题即可。试试："
            "“根据我的高数档案，给我一个期末复习计划”"
            " 或 “记住：我每周三、周五晚上健身”。"
        )

    for message in st.session_state.messages:
        _render_message(message)

    prompt = st.chat_input("输入你的问题……")
    if not prompt:
        return

    prompt = prompt.strip()
    if not prompt:
        return

    user_message = {"role": "user", "content": prompt}
    st.session_state.messages.append(user_message)
    with st.chat_message("user", avatar="🧑"):
        st.markdown(prompt)

    history = list(st.session_state.history[-MAX_HISTORY_MESSAGES:])

    with st.chat_message("assistant", avatar="🤖"):
        status = st.status(
            "🤖 主管正在思考……",
            expanded=True,
            state="running",
        )
        answer_placeholder = st.empty()
        try:
            answer, trace = _stream_answer(
                assistant,
                prompt,
                history,
                status,
                answer_placeholder,
            )
            status.update(label="✅ 完成", state="complete", expanded=False)
            _render_trace(trace)
        except Exception as exc:  # noqa: BLE001 - 单轮失败不应让页面崩溃
            status.update(label="❌ 本轮失败", state="error", expanded=True)
            st.error(f"{type(exc).__name__}: {exc}")
            st.session_state.history.append(HumanMessage(content=prompt))
            _trim_history()
            return

    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": answer,
            "trace": trace,
        }
    )
    st.session_state.history.append(HumanMessage(content=prompt))
    worker_name = trace.get("worker")
    st.session_state.history.append(
        AIMessage(
            content=answer,
            name=worker_name if worker_name in WORKER_NODES else "assistant",
        )
    )
    _trim_history()


if __name__ == "__main__":
    main()
