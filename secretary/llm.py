"""聊天模型初始化，支持 DeepSeek / 智谱（zhipu）切换。

DeepSeek 和智谱都提供 OpenAI 兼容接口，所以直接复用 langchain-openai 的
``ChatOpenAI``，只替换 base_url 和模型名即可，不需要额外 SDK。
provider 由 ``Settings.model_provider`` 决定，默认 deepseek。
"""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI

from .config import (
    Settings,
    get_settings,
    require_api_key,
    require_zhipu_key,
)


def _build_zhipu_model(
    settings: Settings,
    *,
    temperature: float,
    streaming: bool,
) -> ChatOpenAI:
    """创建智谱聊天模型（OpenAI 兼容接口）。"""

    return ChatOpenAI(
        model=settings.zhipu_model,
        api_key=require_zhipu_key(settings),
        base_url=settings.zhipu_base_url,
        temperature=temperature,
        timeout=60,
        max_retries=2,
        streaming=streaming,
    )


def build_chat_model(
    settings: Settings | None = None,
    *,
    temperature: float = 0.0,
    streaming: bool = False,
) -> ChatOpenAI:
    """按 ``settings.model_provider`` 创建聊天模型。

    默认温度为 0，尽量让 Supervisor 的路由结果稳定、可复现。
    ``DEEPSEEK_THINKING`` 和 ``DEEPSEEK_REASONING_EFFORT`` 只对 deepseek
    生效；zhipu 走独立分支，不传这些参数。
    """

    settings = settings or get_settings()

    if settings.model_provider == "zhipu":
        return _build_zhipu_model(
            settings,
            temperature=temperature,
            streaming=streaming,
        )

    api_key = require_api_key(settings)

    kwargs: dict[str, Any] = {}
    if settings.deepseek_thinking in ("enabled", "disabled"):
        kwargs["extra_body"] = {"thinking": {"type": settings.deepseek_thinking}}
    if settings.deepseek_reasoning_effort in ("low", "high", "max"):
        kwargs["reasoning_effort"] = settings.deepseek_reasoning_effort

    return ChatOpenAI(
        model=settings.deepseek_model,
        api_key=api_key,
        base_url=settings.deepseek_base_url,
        temperature=temperature,
        timeout=60,
        max_retries=2,
        streaming=streaming,
        **kwargs,
    )


def require_provider_key(settings: Settings | None = None) -> str:
    """按当前 provider 预检查 Key，供启动入口提前给出可读报错。"""

    settings = settings or get_settings()
    if settings.model_provider == "zhipu":
        return require_zhipu_key(settings)
    return require_api_key(settings)
