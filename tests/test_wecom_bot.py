"""企业微信文本双向对话（Slice 6b3a）的测试。

覆盖三件事：

1. 解密后的 XML 能正确提取 ``FromUserName`` / ``MsgType`` / ``Content``；
2. 文本消息会调用共享的 SecretaryAssistant，并通过通知器定向主动回复；
3. 非文本消息和异常路径不会触发回复，也不会让后台线程拖垮 HTTP 服务。
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from secretary.config import get_settings
from secretary.desk import DeskStore
from secretary.notify import NoopNotifier, WeComNotifier
from secretary.notify.wecom_crypt import WeComCrypt
from secretary.prompts import DESK_EXPERT_PROMPT

# scripts 不是包，按文件路径加载接收消息服务器模块
_BOT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "wecom_bot.py"
_SPEC = importlib.util.spec_from_file_location("wecom_bot_for_bot_test", _BOT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
wecom_bot = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = wecom_bot
_SPEC.loader.exec_module(wecom_bot)

TOKEN = "QDG6eK"
ENCODING_AES_KEY = "jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C"
CORP_ID = "wx5823bf96d3bd56c7"


def _crypt() -> WeComCrypt:
    return WeComCrypt(TOKEN, ENCODING_AES_KEY, CORP_ID)


def _plain_xml(
    *,
    from_user: str = "zhangsan",
    msg_type: str = "text",
    content: str = "你好",
) -> str:
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        "<CreateTime>1700000000</CreateTime>"
        f"<MsgType><![CDATA[{msg_type}]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        "<MsgId>1234567890123456</MsgId>"
        "<AgentID>1000002</AgentID>"
        "</xml>"
    )


def _signed_request(
    plain: str,
    *,
    timestamp: str = "1700000000",
    nonce: str = "nonce-1",
) -> tuple[dict[str, list[str]], bytes]:
    """按企业微信规则加密并签名，返回 handle_message 的参数。"""

    crypt = _crypt()
    encrypt = crypt.encrypt(plain)
    signature = hashlib.sha1(
        "".join(sorted([TOKEN, timestamp, nonce, encrypt])).encode("utf-8")
    ).hexdigest()
    body = (
        "<xml>"
        f"<ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
        f"<Encrypt><![CDATA[{encrypt}]]></Encrypt>"
        "<AgentID><![CDATA[1000002]]></AgentID>"
        "</xml>"
    ).encode()
    params = {
        "msg_signature": [signature],
        "timestamp": [timestamp],
        "nonce": [nonce],
    }
    return params, body


class _FakeAssistant:
    def __init__(self, answer: str = "你好，我是秘书。", error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.questions: list[str] = []
        self.histories: list[list] = []

    def ask(self, question: str) -> str:
        self.questions.append(question)
        if self.error is not None:
            raise self.error
        return self.answer

    def ask_turn(self, question: str, *, history=None):
        self.histories.append(list(history or []))
        answer = self.ask(question)
        messages = [*(history or []), HumanMessage(content=question)]
        if answer:
            messages.append(AIMessage(content=answer, name="desk_expert"))
        return answer, messages


class _FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []

    def send(self, title: str, body: str, user_id: str | None = None) -> bool:
        self.sent.append((title, body, user_id))
        return True


# ---------------------------------------------------------------- XML 解析


def test_parse_message_extracts_sender_type_and_content() -> None:
    message = wecom_bot.parse_message(_plain_xml(content="你好，秘书"))

    assert message is not None
    assert message.from_user == "zhangsan"
    assert message.msg_type == "text"
    assert message.content == "你好，秘书"


def test_parse_message_normalizes_type_case() -> None:
    message = wecom_bot.parse_message(_plain_xml(msg_type="Text"))

    assert message is not None
    assert message.msg_type == "text"


def test_parse_message_invalid_xml_returns_none() -> None:
    assert wecom_bot.parse_message("not xml") is None
    assert wecom_bot.parse_message("<xml>") is None


# -------------------------------------------------------------- 主动回复


def test_reply_to_message_asks_assistant_and_targets_sender() -> None:
    assistant = _FakeAssistant(answer="  已经记下了  ")
    notifier = _FakeNotifier()
    message = wecom_bot.IncomingMessage(
        from_user="zhangsan", msg_type="text", content="提醒我明天下午3点交房租"
    )

    assert wecom_bot.reply_to_message(message, assistant, notifier) is True

    assert assistant.questions == ["提醒我明天下午3点交房租"]
    assert notifier.sent == [("", "已经记下了", "zhangsan")]


def test_reply_to_message_sends_fallback_when_assistant_raises(caplog) -> None:
    assistant = _FakeAssistant(error=RuntimeError("deepseek down"))
    notifier = _FakeNotifier()
    message = wecom_bot.IncomingMessage(
        from_user="zhangsan", msg_type="text", content="你好"
    )

    assert wecom_bot.reply_to_message(message, assistant, notifier) is True

    assert notifier.sent[0][2] == "zhangsan"
    assert notifier.sent[0][1] == wecom_bot.REPLY_ERROR_TEXT
    assert "处理企业微信消息失败" in caplog.text


def test_reply_to_message_sends_fallback_when_answer_empty() -> None:
    assistant = _FakeAssistant(answer="   ")
    notifier = _FakeNotifier()
    message = wecom_bot.IncomingMessage(
        from_user="zhangsan", msg_type="text", content="你好"
    )

    assert wecom_bot.reply_to_message(message, assistant, notifier) is True
    assert notifier.sent[0][1] == wecom_bot.REPLY_ERROR_TEXT


def test_reply_to_message_skips_empty_content() -> None:
    assistant = _FakeAssistant()
    notifier = _FakeNotifier()
    message = wecom_bot.IncomingMessage(
        from_user="zhangsan", msg_type="text", content="   "
    )

    assert wecom_bot.reply_to_message(message, assistant, notifier) is False
    assert assistant.questions == []
    assert notifier.sent == []


def test_reply_to_message_survives_notifier_exception(caplog) -> None:
    class _BoomNotifier:
        def send(self, title: str, body: str, user_id: str | None = None) -> bool:
            raise RuntimeError("wecom down")

    message = wecom_bot.IncomingMessage(
        from_user="zhangsan", msg_type="text", content="你好"
    )

    assert wecom_bot.reply_to_message(message, _FakeAssistant(), _BoomNotifier()) is False
    assert "发送企业微信回复失败" in caplog.text


# ------------------------------------------------------------ 短期上下文


def _text_message(content: str, from_user: str = "zhangsan"):
    return wecom_bot.IncomingMessage(
        from_user=from_user, msg_type="text", content=content
    )


def test_reply_to_message_reuses_previous_turn_history() -> None:
    assistant = _FakeAssistant()
    notifier = _FakeNotifier()
    conversations = wecom_bot.ConversationStore()

    assert wecom_bot.reply_to_message(
        _text_message("第一句"), assistant, notifier, conversations
    )
    assert wecom_bot.reply_to_message(
        _text_message("第二句"), assistant, notifier, conversations
    )

    # 第一轮没有历史；第二轮应看到上一轮的用户消息和最终回答
    assert assistant.histories[0] == []
    assert [message.content for message in assistant.histories[1]] == [
        "第一句",
        "你好，我是秘书。",
    ]


def test_conversation_store_trims_per_user_and_evicts_lru() -> None:
    store = wecom_bot.ConversationStore(max_messages=3, max_users=2)
    store.set("a", [HumanMessage(content=str(index)) for index in range(5)])
    store.set("b", [HumanMessage(content="b")])

    assert [message.content for message in store.get("a")] == ["2", "3", "4"]

    # 访问 a 会刷新 LRU；随后写入 c 时应淘汰最久未使用的 b
    store.get("a")
    store.set("c", [HumanMessage(content="c")])

    assert [message.content for message in store.get("a")] == ["2", "3", "4"]
    assert [message.content for message in store.get("c")] == ["c"]
    assert store.get("b") == []


def test_conversation_store_keeps_lock_after_eviction() -> None:
    store = wecom_bot.ConversationStore(max_messages=2, max_users=1)
    lock_a = store.lock_for("a")
    store.set("a", [HumanMessage(content="a")])
    # 写入 b 触发 a 的 history 被淘汰（max_users=1）
    store.set("b", [HumanMessage(content="b")])
    # 淘汰只删 history，锁必须保留，否则同一用户会出现两把锁
    assert store.lock_for("a") is lock_a
    assert store.get("a") == []


def test_reply_to_message_serializes_same_user() -> None:
    store = wecom_bot.ConversationStore()
    notifier = _FakeNotifier()
    first_started = threading.Event()
    release = threading.Event()
    second_called = threading.Event()
    calls: list[tuple[str, list]] = []
    guard = threading.Lock()

    class _SerialAssistant:
        def ask_turn(self, question: str, *, history=None):
            with guard:
                calls.append((question, list(history or [])))
            if question == "第一条":
                first_started.set()
                release.wait(2)
                answer = "第一条回复"
            else:
                second_called.set()
                answer = "第二条回复"
            return answer, [
                *(history or []),
                HumanMessage(content=question),
                AIMessage(content=answer, name="desk_expert"),
            ]

    assistant = _SerialAssistant()
    first = wecom_bot.dispatch_reply(
        _text_message("第一条"), assistant, notifier, store
    )
    assert first is not None
    assert first_started.wait(2) is True

    second = wecom_bot.dispatch_reply(
        _text_message("第二条"), assistant, notifier, store
    )
    assert second is not None
    # 第一条仍持锁时，第二条不能抢跑
    assert second_called.wait(0.1) is False

    release.set()
    first.join(2)
    second.join(2)
    assert not first.is_alive() and not second.is_alive()

    assert [question for question, _ in calls] == ["第一条", "第二条"]
    assert [message.content for message in calls[1][1]] == [
        "第一条",
        "第一条回复",
    ]
    assert [body for _, body, _ in notifier.sent] == ["第一条回复", "第二条回复"]


def test_two_turn_wecom_dialog_merges_intent_into_one_task(tmp_path) -> None:
    desk = DeskStore(tmp_path / "desk.db")
    conversations = wecom_bot.ConversationStore()

    class _MergeAssistant:
        """模拟“上一轮说要健身、这一轮只补时间”的合并行为。"""

        def __init__(self) -> None:
            self.calls: list[tuple[str, list]] = []

        def ask_turn(self, question: str, *, history=None):
            history = list(history or [])
            self.calls.append((question, history))
            if "健身" in question and not history:
                answer = "想几点去健身？"
                return answer, [
                    HumanMessage(content=question),
                    AIMessage(content=answer, name="life_expert"),
                ]

            previous_intent = next(
                (
                    str(message.content)
                    for message in history
                    if isinstance(message, HumanMessage)
                    and "健身" in str(message.content)
                ),
                "",
            )
            if previous_intent and ("八点半" in question or "20:30" in question):
                desk.add_task("去健身", due_at="2026-11-19 20:30")
                answer = "已新建任务：20:30 去健身"
            else:
                answer = "没听懂，请再说一次"
            return answer, [
                *history,
                HumanMessage(content=question),
                AIMessage(content=answer, name="desk_expert"),
            ]

    assistant = _MergeAssistant()
    notifier = _FakeNotifier()

    assert wecom_bot.reply_to_message(
        _text_message("去健身"), assistant, notifier, conversations
    )
    assert wecom_bot.reply_to_message(
        _text_message("八点半"), assistant, notifier, conversations
    )

    assert len(assistant.calls) == 2
    assert assistant.calls[1][1], "第二轮必须拿到上一轮上下文"
    assert desk.count() == 1
    assert desk.list_tasks()[0].title == "去健身"
    assert "20:30" in notifier.sent[1][1]


def test_desk_prompt_contains_intent_merge_rules() -> None:
    assert "合并成一个完整任务" in DESK_EXPERT_PROMPT
    assert "update_task" in DESK_EXPERT_PROMPT
    assert "最多列出 5 条" in DESK_EXPERT_PROMPT


# ------------------------------------------------------------ 纯文本与截断


def test_plain_text_strips_markdown_but_keeps_lists() -> None:
    raw = (
        "## 标题\n"
        "**加粗** 和 __下划线__\n"
        "- 列表一\n"
        "- `代码`\n"
        "~~删除~~\n\n\n"
        "结尾"
    )

    assert wecom_bot._plain_text(raw) == (
        "标题\n加粗 和 下划线\n- 列表一\n- 代码\n删除\n\n结尾"
    )


def test_truncate_utf8_never_splits_multibyte_characters() -> None:
    text = "中" * 3000

    truncated = wecom_bot._truncate_utf8(text)

    assert len(truncated.encode("utf-8")) <= wecom_bot.WECOM_TEXT_LIMIT_BYTES
    assert truncated.endswith(wecom_bot.TRUNCATE_SUFFIX)
    assert truncated.startswith("中")
    assert "\ufffd" not in truncated


def test_reply_to_message_cleans_markdown_and_truncates() -> None:
    assistant = _FakeAssistant(answer="## 标题\n**加粗**\n" + "中" * 3000)
    notifier = _FakeNotifier()

    assert wecom_bot.reply_to_message(
        _text_message("你好"), assistant, notifier
    )

    body = notifier.sent[0][1]
    assert body.startswith("标题\n加粗")
    assert "**" not in body
    assert "##" not in body
    assert body.endswith(wecom_bot.TRUNCATE_SUFFIX)
    assert len(body.encode("utf-8")) <= wecom_bot.WECOM_TEXT_LIMIT_BYTES


# ---------------------------------------------------------------- 分发规则


@pytest.mark.parametrize(
    "message",
    [
        wecom_bot.IncomingMessage(from_user="", msg_type="text", content="你好"),
        wecom_bot.IncomingMessage(from_user="zhangsan", msg_type="image", content=""),
        wecom_bot.IncomingMessage(from_user="zhangsan", msg_type="voice", content=""),
        wecom_bot.IncomingMessage(from_user="zhangsan", msg_type="text", content="  "),
    ],
)
def test_dispatch_reply_ignores_invalid_messages(message, caplog) -> None:
    assistant = _FakeAssistant()
    notifier = _FakeNotifier()

    assert wecom_bot.dispatch_reply(message, assistant, notifier) is None
    assert assistant.questions == []
    assert notifier.sent == []


def test_dispatch_reply_runs_in_background_thread() -> None:
    started = threading.Event()
    release = threading.Event()

    class _BlockingAssistant:
        def ask_turn(self, question: str, *, history=None):
            started.set()
            release.wait(2)
            return "后台回复", [*(history or []), HumanMessage(content=question)]

    notifier = _FakeNotifier()
    message = wecom_bot.IncomingMessage(
        from_user="zhangsan", msg_type="text", content="你好"
    )

    thread = wecom_bot.dispatch_reply(message, _BlockingAssistant(), notifier)

    assert thread is not None
    assert thread.daemon is True
    assert started.wait(2) is True
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert notifier.sent == [("", "后台回复", "zhangsan")]


# ------------------------------------------------------------ POST 处理链路


def test_handle_message_acks_and_dispatches_text(monkeypatch) -> None:
    captured: list[wecom_bot.IncomingMessage] = []

    def fake_dispatch(message, assistant, notifier):
        captured.append(message)

    monkeypatch.setattr(wecom_bot, "dispatch_reply", fake_dispatch)
    params, body = _signed_request(_plain_xml(content="你好"))
    assistant = _FakeAssistant()
    notifier = _FakeNotifier()

    status, reply = wecom_bot.handle_message(
        _crypt(), params, body, assistant=assistant, notifier=notifier
    )

    # 立即 ACK：不返回被动回复正文
    assert status == 200
    assert reply == ""
    assert len(captured) == 1
    assert captured[0].from_user == "zhangsan"
    assert captured[0].content == "你好"


def test_handle_message_forwards_conversation_store(monkeypatch) -> None:
    captured: list[object] = []

    def fake_dispatch(message, assistant, notifier, conversations=None):
        captured.append(conversations)

    monkeypatch.setattr(wecom_bot, "dispatch_reply", fake_dispatch)
    params, body = _signed_request(_plain_xml(content="你好"))
    conversations = wecom_bot.ConversationStore()

    wecom_bot.handle_message(
        _crypt(),
        params,
        body,
        assistant=_FakeAssistant(),
        notifier=_FakeNotifier(),
        conversations=conversations,
    )

    assert captured == [conversations]


def test_handle_message_non_text_does_not_dispatch() -> None:
    params, body = _signed_request(_plain_xml(msg_type="image", content=""))
    assistant = _FakeAssistant()
    notifier = _FakeNotifier()

    status, reply = wecom_bot.handle_message(
        _crypt(),
        params,
        body,
        assistant=assistant,
        notifier=notifier,
    )

    assert status == 200
    assert reply == ""
    assert assistant.questions == []
    assert notifier.sent == []


def test_handle_message_without_reply_dependencies_still_acks() -> None:
    params, body = _signed_request(_plain_xml())

    status, reply = wecom_bot.handle_message(_crypt(), params, body)

    assert status == 200
    assert reply == ""


def test_handle_message_model_command_switches_inline(monkeypatch) -> None:
    dispatched: list[tuple] = []
    monkeypatch.setattr(
        wecom_bot,
        "dispatch_reply",
        lambda *args, **kwargs: dispatched.append(args),
    )

    class _ModelAssistant(_FakeAssistant):
        def __init__(self) -> None:
            super().__init__()
            self.switched: list[str | None] = []

        def switch_model(self, provider=None, model=None):
            self.switched.append(provider)
            return f"已切换到 {provider}/{model or 'default'}"

    assistant = _ModelAssistant()
    notifier = _FakeNotifier()
    params, body = _signed_request(_plain_xml(content="/model zhipu"))

    status, reply = wecom_bot.handle_message(
        _crypt(), params, body, assistant=assistant, notifier=notifier
    )

    assert status == 200
    assert reply == ""
    assert assistant.switched == ["zhipu"]
    # 同步回复，不进入普通对话线程
    assert assistant.questions == []
    assert notifier.sent == [("", "已切换到 zhipu/default", "zhangsan")]
    assert dispatched == []


def test_handle_message_model_command_replies_failure(monkeypatch) -> None:
    dispatched: list[tuple] = []
    monkeypatch.setattr(
        wecom_bot,
        "dispatch_reply",
        lambda *args, **kwargs: dispatched.append(args),
    )

    class _FailingAssistant(_FakeAssistant):
        def switch_model(self, provider=None, model=None):
            raise RuntimeError("缺少 ZHIPU_API_KEY")

    notifier = _FakeNotifier()
    params, body = _signed_request(_plain_xml(content="/model zhipu"))

    status, reply = wecom_bot.handle_message(
        _crypt(),
        params,
        body,
        assistant=_FailingAssistant(),
        notifier=notifier,
    )

    assert status == 200
    assert reply == ""
    assert notifier.sent == [("", "切换失败：缺少 ZHIPU_API_KEY", "zhangsan")]
    assert dispatched == []


def test_handle_message_effort_command_switches_inline(monkeypatch) -> None:
    dispatched: list[tuple] = []
    monkeypatch.setattr(
        wecom_bot,
        "dispatch_reply",
        lambda *args, **kwargs: dispatched.append(args),
    )

    class _EffortAssistant(_FakeAssistant):
        def __init__(self) -> None:
            super().__init__()
            self.efforts: list[str | None] = []

        def switch_effort(self, effort=None):
            self.efforts.append(effort)
            return f"推理强度已设为 {effort or '默认'}"

    assistant = _EffortAssistant()
    notifier = _FakeNotifier()
    params, body = _signed_request(_plain_xml(content="/effort high"))

    status, reply = wecom_bot.handle_message(
        _crypt(), params, body, assistant=assistant, notifier=notifier
    )

    assert status == 200
    assert reply == ""
    assert assistant.efforts == ["high"]
    # 同步回复，不进入普通对话线程
    assert assistant.questions == []
    assert notifier.sent == [("", "推理强度已设为 high", "zhangsan")]
    assert dispatched == []


def test_handle_message_effort_command_replies_failure(monkeypatch) -> None:
    dispatched: list[tuple] = []
    monkeypatch.setattr(
        wecom_bot,
        "dispatch_reply",
        lambda *args, **kwargs: dispatched.append(args),
    )

    class _FailingAssistant(_FakeAssistant):
        def switch_effort(self, effort=None):
            raise ValueError("当前 provider 不是 deepseek，推理强度只对 deepseek 生效")

    notifier = _FakeNotifier()
    params, body = _signed_request(_plain_xml(content="/effort max"))

    status, reply = wecom_bot.handle_message(
        _crypt(),
        params,
        body,
        assistant=_FailingAssistant(),
        notifier=notifier,
    )

    assert status == 200
    assert reply == ""
    assert notifier.sent == [
        (
            "",
            "设置失败：当前 provider 不是 deepseek，推理强度只对 deepseek 生效",
            "zhangsan",
        )
    ]
    assert dispatched == []


# ---------------------------------------------------------------- 构造依赖


def test_build_reply_notifier_returns_wecom_when_configured() -> None:
    settings = replace(
        get_settings(),
        wecom_corp_id="corp",
        wecom_agent_id="1000002",
        wecom_secret="secret",
        wecom_user_id="zhangsan",
    )

    notifier = wecom_bot.build_reply_notifier(settings)

    assert isinstance(notifier, WeComNotifier)
    assert notifier._user_id == "zhangsan"


def test_build_reply_notifier_falls_back_when_incomplete(caplog) -> None:
    settings = replace(
        get_settings(),
        wecom_corp_id="corp",
        wecom_agent_id="",
        wecom_secret="",
    )

    notifier = wecom_bot.build_reply_notifier(settings)

    assert isinstance(notifier, NoopNotifier)
    assert "WECOM_AGENT_ID" in caplog.text
    assert "WECOM_SECRET" in caplog.text
