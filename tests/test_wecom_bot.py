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

from secretary.config import get_settings
from secretary.notify import NoopNotifier, WeComNotifier
from secretary.notify.wecom_crypt import WeComCrypt

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

    def ask(self, question: str) -> str:
        self.questions.append(question)
        if self.error is not None:
            raise self.error
        return self.answer


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
        def ask(self, question: str) -> str:
            started.set()
            release.wait(2)
            return "后台回复"

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
