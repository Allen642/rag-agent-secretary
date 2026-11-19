"""对话内切换模型：只重建 llm + graph，复用知识库和工作台。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from secretary import graph as graph_module
from secretary.config import get_settings
from secretary.demo_model import DemoChatModel
from secretary.desk import DeskStore
from secretary.graph import SecretaryAssistant
from secretary.rag import ChromaKnowledgeBase


def _make_assistant(tmp_path, monkeypatch) -> tuple[SecretaryAssistant, list]:
    """构造离线助理，并记录 switch_model 里 build_chat_model 的调用。"""

    settings = replace(
        get_settings(),
        model_provider="deepseek",
        deepseek_api_key="sk-test",
        zhipu_api_key="zhipu-test-key",
    )
    built: list = []

    def fake_build(settings=None, **kwargs):
        model = DemoChatModel()
        built.append((settings, model))
        return model

    monkeypatch.setattr(graph_module, "build_chat_model", fake_build)
    assistant = SecretaryAssistant(
        llm=DemoChatModel(),
        settings=settings,
        knowledge_base=ChromaKnowledgeBase(
            persist_dir=tmp_path / "chroma",
            collection_name="model_switch_test",
        ),
        desk_store=DeskStore(tmp_path / "desk.db"),
        enable_auto_memory=False,
    )
    return assistant, built


def test_switch_model_to_zhipu_rebuilds_llm_and_graph(tmp_path, monkeypatch) -> None:
    assistant, built = _make_assistant(tmp_path, monkeypatch)
    old_llm = assistant.llm
    old_graph = assistant.graph
    knowledge_base = assistant.knowledge_base
    desk_store = assistant.desk_store

    result = assistant.switch_model("zhipu")

    assert result == f"已切换到 zhipu/{assistant.settings.zhipu_model}"
    assert assistant.settings.model_provider == "zhipu"
    assert built[-1][0].model_provider == "zhipu"
    assert assistant.llm is built[-1][1]
    assert assistant.llm is not old_llm
    assert assistant.graph is not old_graph
    # 切换只重建 llm + graph，重资源对象必须复用
    assert assistant.knowledge_base is knowledge_base
    assert assistant.desk_store is desk_store


def test_switch_model_overrides_provider_model_name(tmp_path, monkeypatch) -> None:
    assistant, built = _make_assistant(tmp_path, monkeypatch)

    result = assistant.switch_model("ZHIPU", "glm-4-plus")

    assert result == "已切换到 zhipu/glm-4-plus"
    assert assistant.settings.zhipu_model == "glm-4-plus"
    assert built[-1][0].zhipu_model == "glm-4-plus"


def test_switch_model_rejects_unknown_provider(tmp_path, monkeypatch) -> None:
    assistant, built = _make_assistant(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="不支持的 provider"):
        assistant.switch_model("bogus")

    assert assistant.settings.model_provider == "deepseek"
    assert built == []


def test_switch_model_failure_keeps_previous_state(tmp_path, monkeypatch) -> None:
    assistant, _ = _make_assistant(tmp_path, monkeypatch)
    old_settings = assistant.settings
    old_llm = assistant.llm
    old_graph = assistant.graph

    def failing_build(settings=None, **kwargs):
        raise RuntimeError("缺少 ZHIPU_API_KEY")

    monkeypatch.setattr(graph_module, "build_chat_model", failing_build)

    with pytest.raises(RuntimeError, match="ZHIPU_API_KEY"):
        assistant.switch_model("zhipu")

    assert assistant.settings is old_settings
    assert assistant.llm is old_llm
    assert assistant.graph is old_graph


def test_switch_effort_rebuilds_llm_and_graph(tmp_path, monkeypatch) -> None:
    assistant, built = _make_assistant(tmp_path, monkeypatch)
    old_llm = assistant.llm
    old_graph = assistant.graph
    knowledge_base = assistant.knowledge_base
    desk_store = assistant.desk_store

    result = assistant.switch_effort("high")

    assert result == "推理强度已设为 high"
    assert assistant.settings.deepseek_reasoning_effort == "high"
    assert built[-1][0].deepseek_reasoning_effort == "high"
    assert assistant.llm is built[-1][1]
    assert assistant.llm is not old_llm
    assert assistant.graph is not old_graph
    # 只重建 llm + graph，重资源对象复用
    assert assistant.knowledge_base is knowledge_base
    assert assistant.desk_store is desk_store


def test_switch_effort_clears_with_empty_value(tmp_path, monkeypatch) -> None:
    assistant, built = _make_assistant(tmp_path, monkeypatch)
    assistant.switch_effort("max")

    result = assistant.switch_effort(None)

    assert result == "推理强度已设为 默认"
    assert assistant.settings.deepseek_reasoning_effort == ""
    assert built[-1][0].deepseek_reasoning_effort == ""


def test_switch_effort_requires_deepseek_provider(tmp_path, monkeypatch) -> None:
    assistant, built = _make_assistant(tmp_path, monkeypatch)
    assistant.switch_model("zhipu")
    old_settings = assistant.settings
    old_llm = assistant.llm
    old_graph = assistant.graph
    calls_after_switch = len(built)

    with pytest.raises(ValueError, match="不是 deepseek"):
        assistant.switch_effort("high")

    assert assistant.settings is old_settings
    assert assistant.llm is old_llm
    assert assistant.graph is old_graph
    assert len(built) == calls_after_switch


def test_switch_effort_rejects_invalid_level(tmp_path, monkeypatch) -> None:
    assistant, built = _make_assistant(tmp_path, monkeypatch)
    old_settings = assistant.settings
    old_llm = assistant.llm
    old_graph = assistant.graph

    with pytest.raises(ValueError, match="推理强度只能是 low/high/max"):
        assistant.switch_effort("ultra")

    # 失败时状态完全不变，也没有触发重建
    assert assistant.settings is old_settings
    assert assistant.llm is old_llm
    assert assistant.graph is old_graph
    assert built == []


def test_switch_effort_failure_keeps_previous_state(tmp_path, monkeypatch) -> None:
    assistant, _ = _make_assistant(tmp_path, monkeypatch)
    old_settings = assistant.settings
    old_llm = assistant.llm
    old_graph = assistant.graph

    def failing_build(settings=None, **kwargs):
        raise RuntimeError("缺少 DEEPSEEK_API_KEY")

    monkeypatch.setattr(graph_module, "build_chat_model", failing_build)

    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        assistant.switch_effort("max")

    assert assistant.settings is old_settings
    assert assistant.llm is old_llm
    assert assistant.graph is old_graph
