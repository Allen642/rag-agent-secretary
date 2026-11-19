"""DeepSeek 模型构建测试：推理模式参数必须按配置正确透传。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from secretary.config import get_settings
from secretary.llm import build_chat_model


def _settings(**overrides):
    """构造一个带测试 Key 的配置，避免依赖真实 .env。"""

    base = replace(
        get_settings(),
        model_provider="deepseek",
        deepseek_api_key="sk-test",
        zhipu_api_key=None,
    )
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


def test_build_chat_model_uses_zhipu_when_configured() -> None:
    model = build_chat_model(
        _settings(
            model_provider="zhipu",
            zhipu_api_key="zhipu-test-key",
            zhipu_base_url="https://open.bigmodel.cn/api/paas/v4",
            zhipu_model="glm-4-flash",
        )
    )

    assert model.model_name == "glm-4-flash"
    assert model.openai_api_base == "https://open.bigmodel.cn/api/paas/v4"
    assert model.openai_api_key.get_secret_value() == "zhipu-test-key"


def test_build_chat_model_zhipu_ignores_deepseek_thinking() -> None:
    """思考模式参数只对 deepseek 生效，不能漏传给智谱。"""

    model = build_chat_model(
        _settings(
            model_provider="zhipu",
            zhipu_api_key="zhipu-test-key",
            deepseek_thinking="enabled",
            deepseek_reasoning_effort="high",
        )
    )

    assert not model.extra_body
    assert model.reasoning_effort is None


def test_build_chat_model_zhipu_without_key_raises() -> None:
    with pytest.raises(RuntimeError, match="ZHIPU_API_KEY"):
        build_chat_model(_settings(model_provider="zhipu", zhipu_api_key=None))


def test_get_settings_reads_provider_and_zhipu_env(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "  ZHIPU ")
    monkeypatch.setenv("ZHIPU_API_KEY", " zhipu-env-key ")
    monkeypatch.setenv("ZHIPU_BASE_URL", "https://example.com/v4")
    monkeypatch.setenv("ZHIPU_MODEL", "glm-4-plus")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.model_provider == "zhipu"
        assert settings.zhipu_api_key == "zhipu-env-key"
        assert settings.zhipu_base_url == "https://example.com/v4"
        assert settings.zhipu_model == "glm-4-plus"
    finally:
        get_settings.cache_clear()


def test_get_settings_provider_defaults_to_deepseek(monkeypatch) -> None:
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.model_provider == "deepseek"
        assert settings.zhipu_model == "glm-4-flash"
    finally:
        get_settings.cache_clear()
