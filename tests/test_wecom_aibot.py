"""``scripts/wecom_aibot.py`` 的测试。

覆盖 ``--check`` 配置校验分支，以及 ``make_sender`` 对普通消息与
``enter_chat`` 事件的分派：不构造助理、不联网，避免依赖真实 Bot 凭证。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

from secretary.config import get_settings

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "wecom_aibot.py"
_SPEC = importlib.util.spec_from_file_location("wecom_aibot_for_test", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
wecom_aibot = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = wecom_aibot
_SPEC.loader.exec_module(wecom_aibot)


def _fixed_settings(*, bot_id: str, secret: str):
    """在真实配置基础上替换智能机器人两项，避免读写 .env。"""

    return replace(
        get_settings(),
        wecom_aibot_bot_id=bot_id,
        wecom_aibot_secret=secret,
    )


def test_check_fails_with_clear_message_when_missing(monkeypatch, capsys) -> None:
    settings = _fixed_settings(bot_id="", secret="")
    monkeypatch.setattr(wecom_aibot, "get_settings", lambda: settings)

    assert wecom_aibot.main(["--check"]) == 1

    err = capsys.readouterr().err
    assert "WECOM_AIBOT_BOT_ID" in err
    assert "WECOM_AIBOT_SECRET" in err


def test_check_reports_only_missing_item(monkeypatch, capsys) -> None:
    settings = _fixed_settings(bot_id="bot-123", secret="")
    monkeypatch.setattr(wecom_aibot, "get_settings", lambda: settings)

    assert wecom_aibot.main(["--check"]) == 1

    err = capsys.readouterr().err
    assert "WECOM_AIBOT_SECRET" in err
    assert "WECOM_AIBOT_BOT_ID、" not in err


def test_check_passes_when_configured(monkeypatch, capsys) -> None:
    settings = _fixed_settings(bot_id="bot-123", secret="secret-xyz")
    monkeypatch.setattr(wecom_aibot, "get_settings", lambda: settings)

    assert wecom_aibot.main(["--check"]) == 0

    out = capsys.readouterr().out
    assert "配置检查通过" in out
    assert "bot-123" in out
    # Secret 不进入任何输出
    assert "secret-xyz" not in out


class FakeClient:
    """记录 reply_stream / reply_welcome 调用参数的假客户端。"""

    def __init__(self) -> None:
        self.stream_calls: list[tuple] = []
        self.welcome_calls: list[tuple] = []

    async def reply_stream(self, frame, stream_id, content, finish=False) -> None:
        self.stream_calls.append((frame, stream_id, content, finish))

    async def reply_welcome(self, frame, body) -> None:
        self.welcome_calls.append((frame, body))


def test_sender_uses_reply_stream_for_message() -> None:
    client = FakeClient()
    sender = wecom_aibot.make_sender(client, new_stream_id=lambda: "stream-1")
    frame = {"cmd": "aibot_msg_callback"}

    asyncio.run(sender(frame, "明天下午三点开会"))

    assert len(client.stream_calls) == 1
    sent_frame, stream_id, content, finish = client.stream_calls[0]
    assert sent_frame is frame
    assert stream_id == "stream-1"
    assert content == "明天下午三点开会"
    assert finish is True
    assert client.welcome_calls == []


def test_sender_uses_reply_welcome_for_event() -> None:
    client = FakeClient()
    sender = wecom_aibot.make_sender(client, new_stream_id=lambda: "stream-1")
    frame = {"cmd": "aibot_event_callback"}

    asyncio.run(sender(frame, "你好，我是你的私人秘书。"))

    assert len(client.welcome_calls) == 1
    welcome_frame, body = client.welcome_calls[0]
    assert welcome_frame is frame
    assert body == {
        "msgtype": "text",
        "text": {"content": "你好，我是你的私人秘书。"},
    }
    assert client.stream_calls == []
