"""消息展示相关的小工具，供 Supervisor 和命令行入口复用。"""

from __future__ import annotations

from langchain_core.messages import BaseMessage


def content_to_text(content: object) -> str:
    """把字符串或多模态内容块统一转成可读文本。"""

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                parts.append(str(block["text"]))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


def message_text(message: BaseMessage) -> str:
    """取消息文本并去掉首尾空白。"""

    return content_to_text(message.content).strip()


def truncate(text: str, limit: int = 800) -> str:
    """限制展示长度，避免终端被超长工具结果刷屏。"""

    return text if len(text) <= limit else text[:limit] + "……"
