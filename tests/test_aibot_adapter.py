"""企业微信智能机器人长连接适配层（aibot_adapter）的测试。

全部使用假的 assistant / sender / 内存版 ConversationStore，不联网：
帧解析、去重、同用户串行、历史透传、命令分流、兜底文案、UTF-8 截断和
进入会话欢迎语都在这里覆盖。测试用同步函数 + ``asyncio.run``，不引入
``pytest-asyncio``。
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from secretary.aibot_adapter import (
    CODE_UNSUPPORTED_TEXT,
    REPLY_ERROR_TEXT,
    WELCOME_TEXT,
    AibotRunner,
    parse_text_frame,
)
from secretary.conversation_store import ConversationStore


def make_frame(
    *,
    text: str = "你好",
    msgid: str = "msg-1",
    userid: str | None = "zhangsan",
    chatid: str = "chat-1",
    chattype: str = "single",
) -> dict:
    """构造一条单聊文本消息帧。"""

    body: dict = {
        "msgid": msgid,
        "chattype": chattype,
        "chatid": chatid,
        "msgtype": "text",
        "text": {"content": text},
    }
    if userid is not None:
        body["from"] = {"userid": userid}
    return {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": f"req-{msgid}"},
        "body": body,
    }


class FakeAssistant:
    """记录调用参数的助理替身。"""

    def __init__(self, answer: str = "好的", *, error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.turns: list[tuple[str, list]] = []
        self.model_calls: list[str | None] = []
        self.effort_calls: list[str | None] = []

    def ask_turn(self, question: str, *, history=None):
        self.turns.append((question, list(history or [])))
        if self.error is not None:
            raise self.error
        new_history = [
            *(history or []),
            HumanMessage(content=question),
            AIMessage(content=self.answer),
        ]
        return self.answer, new_history

    def switch_model(self, provider: str | None = None) -> str:
        self.model_calls.append(provider)
        return f"已切换到 {provider or '当前'}"

    def switch_effort(self, effort: str | None = None) -> str:
        self.effort_calls.append(effort)
        return f"推理强度已设为 {effort or '默认'}"


class FakeSender:
    """收集回复的 async sender。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.sent: list[tuple[dict, str]] = []
        self.error = error

    async def __call__(self, frame: dict, text: str) -> None:
        self.sent.append((frame, text))
        if self.error is not None:
            raise self.error


def make_runner(
    assistant: FakeAssistant | None = None,
    *,
    sender: FakeSender | None = None,
    conversations: ConversationStore | None = None,
    reply_limit_bytes: int = 32768,
) -> tuple[AibotRunner, FakeAssistant, FakeSender, ConversationStore]:
    assistant = assistant or FakeAssistant()
    sender = sender or FakeSender()
    conversations = conversations or ConversationStore()
    runner = AibotRunner(
        assistant,
        conversations,
        sender,
        reply_limit_bytes=reply_limit_bytes,
    )
    return runner, assistant, sender, conversations


def run(coro) -> None:
    asyncio.run(coro)


# ---- 帧解析 ----


def test_parse_text_frame_single_chat() -> None:
    frame = make_frame(text="明天几点开会？", msgid="m-1", userid="zhangsan")

    message = parse_text_frame(frame)

    assert message is not None
    assert message.user_id == "zhangsan"
    assert message.text == "明天几点开会？"
    assert message.msgid == "m-1"
    assert message.chat_type == "single"
    assert message.chat_id == "chat-1"
    assert message.frame is frame


def test_parse_text_frame_falls_back_to_chatid() -> None:
    message = parse_text_frame(make_frame(userid=None, chatid="chat-only"))

    assert message is not None
    assert message.user_id == "chat-only"


def test_parse_text_frame_group_chat_keeps_chatid() -> None:
    message = parse_text_frame(
        make_frame(chattype="group", chatid="group-9", userid="lisi")
    )

    assert message is not None
    assert message.chat_type == "group"
    assert message.chat_id == "group-9"
    assert message.user_id == "lisi"


@pytest.mark.parametrize(
    "frame",
    [
        # 事件帧
        {
            "cmd": "aibot_event_callback",
            "headers": {"req_id": "req-1"},
            "body": {"msgtype": "event", "event": {"eventtype": "enter_chat"}},
        },
        # 非文本消息
        {
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "req-2"},
            "body": {"msgid": "m-2", "msgtype": "image", "image": {"url": "x"}},
        },
        # 空文本
        make_frame(text="   "),
        # 缺少 text 节点
        {
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "req-3"},
            "body": {"msgid": "m-3", "msgtype": "text"},
        },
        # 缺少 cmd
        {"headers": {"req_id": "req-4"}, "body": make_frame()["body"]},
        # 非 dict
        None,
        [],
    ],
)
def test_parse_text_frame_returns_none(frame) -> None:
    assert parse_text_frame(frame) is None


# ---- AibotRunner ----


def test_duplicate_msgid_processed_once() -> None:
    runner, assistant, sender, _ = make_runner()
    frame = make_frame(msgid="dup-1")

    run(runner.handle_frame(frame))
    run(runner.handle_frame(frame))

    assert len(assistant.turns) == 1
    assert len(sender.sent) == 1


def test_same_user_history_is_passed_to_next_turn() -> None:
    runner, assistant, sender, _ = make_runner()

    run(runner.handle_frame(make_frame(text="第一轮", msgid="m-1")))
    run(runner.handle_frame(make_frame(text="第二轮", msgid="m-2")))

    assert [question for question, _ in assistant.turns] == ["第一轮", "第二轮"]
    first_history = assistant.turns[0][1]
    second_history = assistant.turns[1][1]
    assert first_history == []
    assert second_history == [
        HumanMessage(content="第一轮"),
        AIMessage(content="好的"),
    ]
    assert sender.sent[1][1] == "好的"


def test_assistant_error_sends_fallback() -> None:
    assistant = FakeAssistant(error=RuntimeError("boom"))
    runner, _, sender, _ = make_runner(assistant)

    run(runner.handle_frame(make_frame(msgid="err-1")))

    assert len(sender.sent) == 1
    assert sender.sent[0][1] == REPLY_ERROR_TEXT


def test_model_command_switches_without_ask_turn() -> None:
    runner, assistant, sender, _ = make_runner()

    run(runner.handle_frame(make_frame(text="/model zhipu", msgid="cmd-1")))

    assert assistant.model_calls == ["zhipu"]
    assert assistant.turns == []
    assert sender.sent[0][1] == "已切换到 zhipu"


def test_effort_command_switches_without_ask_turn() -> None:
    runner, assistant, sender, _ = make_runner()

    run(runner.handle_frame(make_frame(text="/effort high", msgid="cmd-2")))

    assert assistant.effort_calls == ["high"]
    assert assistant.turns == []
    assert sender.sent[0][1] == "推理强度已设为 high"


def test_code_command_is_not_supported() -> None:
    runner, assistant, sender, _ = make_runner()

    run(runner.handle_frame(make_frame(text="/code 加个功能", msgid="cmd-3")))

    assert assistant.turns == []
    assert sender.sent[0][1] == CODE_UNSUPPORTED_TEXT


def test_long_answer_truncated_to_reply_limit() -> None:
    runner, _, sender, _ = make_runner(
        FakeAssistant(answer="字" * 100), reply_limit_bytes=20
    )

    run(runner.handle_frame(make_frame(msgid="long-1")))

    text = sender.sent[0][1]
    assert len(text.encode("utf-8")) <= 20
    assert text.endswith("……")


def test_enter_chat_sends_welcome() -> None:
    runner, _, sender, _ = make_runner()
    frame = {
        "cmd": "aibot_event_callback",
        "headers": {"req_id": "evt-1"},
        "body": {
            "msgtype": "event",
            "event": {"eventtype": "enter_chat"},
            "from": {"userid": "zhangsan"},
        },
    }

    run(runner.handle_enter_chat(frame))

    assert sender.sent == [(frame, WELCOME_TEXT)]


def test_sender_error_does_not_raise() -> None:
    sender = FakeSender(error=RuntimeError("network down"))
    runner, _, _, _ = make_runner(sender=sender)

    # 发送失败只记日志，不能把异常抛给 SDK 的事件循环
    run(runner.handle_frame(make_frame(msgid="send-err")))

    assert len(sender.sent) == 1


def test_duplicate_history_isolated_per_user() -> None:
    runner, assistant, _, _ = make_runner()

    run(runner.handle_frame(make_frame(text="甲", msgid="u1-1", userid="u1")))
    run(runner.handle_frame(make_frame(text="乙", msgid="u2-1", userid="u2")))

    assert assistant.turns[1][1] == []
