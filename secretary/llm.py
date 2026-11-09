"""DeepSeek 模型初始化。

DeepSeek 提供 OpenAI 兼容接口，所以直接复用 langchain-openai 的
``ChatOpenAI``，只替换 base_url 和模型名即可，不需要额外 SDK。
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from .config import Settings, get_settings, require_api_key


def build_chat_model(
    settings: Settings | None = None,
    *,
    temperature: float = 0.0,
    streaming: bool = False,
) -> ChatOpenAI:
    """创建 DeepSeek 聊天模型。

    默认温度为 0，尽量让 Supervisor 的路由结果稳定、可复现。
    """

    settings = settings or get_settings()
    api_key = require_api_key(settings)

    return ChatOpenAI(
        model=settings.deepseek_model,
        api_key=api_key,
        base_url=settings.deepseek_base_url,
        temperature=temperature,
        timeout=60,
        max_retries=2,
        streaming=streaming,
    )
