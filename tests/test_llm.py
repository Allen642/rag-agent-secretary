"""DeepSeek 模型构建测试：推理模式参数必须按配置正确透传。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from secretary.config import get_settings
from secretary.llm import build_chat_model


def _settings(**overrides):
    """构造一个带测试 Key 的配置，避免依赖真实 .env。"""

    base = replace(get_settings(), deepseek_api_key="sk-test")
    return replace(base, **overrides)


def test_build_chat_model_uses_defaults_without_thinking_params() -> None:
    model = build_chat_model(
        _settings(deepseek_thinking="default", deepseek_reasoning_effort="")
    )

    assert model.model_name == get_settings().deepseek_model
    # 默认不显式传参，保持服务端默认行为
    assert not model.extra_body
    assert model.reasoning_effort is None


@pytest.mark.parametrize("thinking", ["enabled", "disabled"])
def test_build_chat_model_passes_thinking_switch(thinking: str) -> None:
    model = build_chat_model(_settings(deepseek_thinking=thinking))

    assert model.extra_body == {"thinking": {"type": thinking}}


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_build_chat_model_passes_reasoning_effort(effort: str) -> None:
    model = build_chat_model(_settings(deepseek_reasoning_effort=effort))

    assert model.reasoning_effort == effort


def test_build_chat_model_ignores_unknown_values() -> None:
    model = build_chat_model(
        _settings(deepseek_thinking="on", deepseek_reasoning_effort="medium")
    )

    assert not model.extra_body
    assert model.reasoning_effort is None


def test_get_settings_reads_thinking_env(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_THINKING", "ENABLED")
    monkeypatch.setenv("DEEPSEEK_REASONING_EFFORT", "MAX")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.deepseek_thinking == "enabled"
        assert settings.deepseek_reasoning_effort == "max"
    finally:
        get_settings.cache_clear()
