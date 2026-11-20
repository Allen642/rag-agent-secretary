"""企业微信推送与 daemon 集成的测试（全部 mock urllib，不访问网络）。"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Self
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import pytest

import secretary.notify.wecom as wecom_module
from secretary.config import get_settings
from secretary.desk import DeskStore
from secretary.notify import (
    NoopNotifier,
    WeComGroupRobotNotifier,
    WeComNotifier,
    make_notifier,
)

# scripts 不是包，按文件路径加载 daemon 模块
_DAEMON_PATH = Path(__file__).resolve().parents[1] / "scripts" / "remind_daemon.py"
_SPEC = importlib.util.spec_from_file_location("remind_daemon_for_notify_test", _DAEMON_PATH)
assert _SPEC is not None and _SPEC.loader is not None
remind_daemon = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(remind_daemon)


class _FakeResponse:
    """urlopen 返回值的替身：支持 with 和 read。"""

    def __init__(self, payload: dict | bytes) -> None:
        if isinstance(payload, bytes):
            self._body = payload
        else:
            self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


class _Recorder:
    """记录请求，并按顺序返回预设响应。"""

    def __init__(self, responses: list[dict | bytes | Exception]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[object, float | None]] = []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item)


@pytest.fixture()
def install_urlopen(monkeypatch):
    """把 wecom 模块里的 urlopen 替换成可编排的 fake。"""

    def install(*responses: dict | bytes | Exception) -> _Recorder:
        recorder = _Recorder(list(responses))
        monkeypatch.setattr(wecom_module.urllib.request, "urlopen", recorder)
        return recorder

    return install


def _notifier(**kwargs) -> WeComNotifier:
    params = {
        "corp_id": "corp-1",
        "agent_id": "1000002",
        "secret": "secret-1",
        "user_id": "zhangsan",
    }
    params.update(kwargs)
    return WeComNotifier(**params)


TOKEN_RESPONSE = {"errcode": 0, "access_token": "TOKEN-1", "expires_in": 7200}
OK_RESPONSE = {"errcode": 0, "errmsg": "ok"}


# --------------------------------------------------------------- 请求内容


def test_send_fetches_token_then_posts_text(install_urlopen) -> None:
    recorder = install_urlopen(TOKEN_RESPONSE, OK_RESPONSE)

    assert _notifier().send("交房租", "截止：2026-11-20 09:00") is True
    assert len(recorder.requests) == 2

    token_request, token_timeout = recorder.requests[0]
    token_url = token_request.full_url  # type: ignore[attr-defined]
    assert token_url.startswith("https://qyapi.weixin.qq.com/cgi-bin/gettoken?")
    query = parse_qs(urlsplit(token_url).query)
    assert query["corpid"] == ["corp-1"]
    assert query["corpsecret"] == ["secret-1"]
    assert token_request.get_method() == "GET"  # type: ignore[attr-defined]
    assert token_request.data is None  # type: ignore[attr-defined]
    assert token_timeout == wecom_module.REQUEST_TIMEOUT_SECONDS

    message_request, _ = recorder.requests[1]
    message_url = message_request.full_url  # type: ignore[attr-defined]
    assert message_url.startswith(
        "https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=TOKEN-1"
    )
    assert message_request.get_method() == "POST"  # type: ignore[attr-defined]

    payload = json.loads(message_request.data.decode("utf-8"))  # type: ignore[attr-defined]
    assert payload == {
        "touser": "zhangsan",
        "msgtype": "text",
        "agentid": 1000002,
        "text": {"content": "交房租\n截止：2026-11-20 09:00"},
    }


def test_default_touser_is_all(install_urlopen) -> None:
    recorder = install_urlopen(TOKEN_RESPONSE, OK_RESPONSE)

    assert _notifier(user_id="").send("标题", "正文") is True

    payload = json.loads(recorder.requests[1][0].data.decode("utf-8"))
    assert payload["touser"] == "@all"


def test_send_can_override_recipient(install_urlopen) -> None:
    recorder = install_urlopen(TOKEN_RESPONSE, OK_RESPONSE)

    assert _notifier().send("", "你好，我是秘书", user_id="lisi") is True

    payload = json.loads(recorder.requests[1][0].data.decode("utf-8"))
    assert payload["touser"] == "lisi"
    # 空标题不会在正文前留下多余空行
    assert payload["text"]["content"] == "你好，我是秘书"


def test_send_blank_override_falls_back_to_default(install_urlopen) -> None:
    recorder = install_urlopen(TOKEN_RESPONSE, OK_RESPONSE)

    assert _notifier().send("标题", "正文", user_id="  ") is True

    payload = json.loads(recorder.requests[1][0].data.decode("utf-8"))
    assert payload["touser"] == "zhangsan"


# --------------------------------------------------------------- token 缓存


def test_token_is_cached_between_sends(install_urlopen) -> None:
    recorder = install_urlopen(TOKEN_RESPONSE, OK_RESPONSE, OK_RESPONSE)
    notifier = _notifier()

    assert notifier.send("第一条", "正文") is True
    assert notifier.send("第二条", "正文") is True

    assert len(recorder.requests) == 3
    gettoken_calls = [
        request
        for request, _ in recorder.requests
        if "/cgi-bin/gettoken" in request.full_url
    ]
    assert len(gettoken_calls) == 1
    assert notifier._access_token == "TOKEN-1"


def test_token_refreshes_after_expiry(install_urlopen) -> None:
    now = [1000.0]
    recorder = install_urlopen(
        TOKEN_RESPONSE,
        OK_RESPONSE,
        {"errcode": 0, "access_token": "TOKEN-2", "expires_in": 7200},
        OK_RESPONSE,
    )
    notifier = _notifier(clock=lambda: now[0])

    assert notifier.send("第一条", "") is True
    # 缓存有效期 = 7200 - 300 秒，这里刚好越过刷新点
    now[0] += wecom_module.TOKEN_TTL_SECONDS - wecom_module.TOKEN_REFRESH_MARGIN_SECONDS + 1
    assert notifier.send("第二条", "") is True

    gettoken_calls = [
        request.full_url
        for request, _ in recorder.requests
        if "/cgi-bin/gettoken" in request.full_url
    ]
    assert len(gettoken_calls) == 2
    assert notifier._access_token == "TOKEN-2"


# ------------------------------------------------------------------- 失败


def test_gettoken_error_returns_false(install_urlopen) -> None:
    recorder = install_urlopen({"errcode": 40013, "errmsg": "invalid corpid"})

    assert _notifier().send("标题", "正文") is False
    assert len(recorder.requests) == 1


def test_network_error_returns_false(install_urlopen, caplog) -> None:
    recorder = install_urlopen(URLError("boom"))

    assert _notifier().send("标题", "正文") is False
    assert len(recorder.requests) == 1
    assert "企业微信请求失败" in caplog.text


def test_send_error_returns_false(install_urlopen) -> None:
    install_urlopen(TOKEN_RESPONSE, {"errcode": 81013, "errmsg": "no permission"})

    assert _notifier().send("标题", "正文") is False


def test_invalid_json_returns_false(install_urlopen) -> None:
    install_urlopen(TOKEN_RESPONSE, b"not json")

    assert _notifier().send("标题", "正文") is False


# ------------------------------------------------------------- 渠道选择


def test_make_notifier_none_returns_noop() -> None:
    settings = replace(
        get_settings(),
        notify_channel="none",
        wecom_corp_id="corp",
        wecom_agent_id="1000002",
        wecom_secret="secret",
    )
    assert isinstance(make_notifier(settings), NoopNotifier)


def test_make_notifier_wecom_returns_client() -> None:
    settings = replace(
        get_settings(),
        notify_channel="wecom",
        wecom_corp_id="corp",
        wecom_agent_id="1000002",
        wecom_secret="secret",
        wecom_user_id="zhangsan",
    )
    notifier = make_notifier(settings)

    assert isinstance(notifier, WeComNotifier)
    assert notifier._agent_id == 1000002
    assert notifier._user_id == "zhangsan"


def test_make_notifier_wecom_robot_returns_robot_client() -> None:
    settings = replace(
        get_settings(),
        notify_channel="wecom_robot",
        wecom_robot_webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k",
    )

    notifier = make_notifier(settings)

    assert isinstance(notifier, WeComGroupRobotNotifier)
    assert notifier._webhook_url.endswith("key=k")


def test_make_notifier_wecom_robot_without_webhook_falls_back(caplog) -> None:
    settings = replace(
        get_settings(),
        notify_channel="wecom_robot",
        wecom_robot_webhook="  ",
    )

    assert isinstance(make_notifier(settings), NoopNotifier)
    assert "WECOM_ROBOT_WEBHOOK" in caplog.text


def test_make_notifier_incomplete_or_unknown_falls_back(caplog) -> None:
    incomplete = replace(
        get_settings(),
        notify_channel="wecom",
        wecom_corp_id="corp",
        wecom_agent_id="1000002",
        wecom_secret="",
    )
    unknown = replace(get_settings(), notify_channel="telegram")

    assert isinstance(make_notifier(incomplete), NoopNotifier)
    assert isinstance(make_notifier(unknown), NoopNotifier)
    assert "配置不完整" in caplog.text
    assert "未知通知渠道" in caplog.text


def test_noop_notifier_never_fails() -> None:
    assert NoopNotifier().send("标题", "正文") is True


# ---------------------------------------------------------- daemon 集成


class _RecorderNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, title: str, body: str) -> bool:
        self.sent.append((title, body))
        return True


def _ts(hour: int, minute: int = 0, *, day: int = 18) -> int:
    return int(datetime(2026, 11, day, hour, minute).timestamp())


def test_tick_pushes_reminder_and_missed(tmp_path) -> None:
    store = DeskStore(tmp_path / "desk.db")
    store.add_task("开会", due_at=datetime(2026, 11, 18, 15, 0), remind_minutes=30)
    notifier = _RecorderNotifier()

    remind_daemon._tick(store, _ts(14, 30), notifier=notifier)
    assert notifier.sent == [("开会", "截止：2026-11-18 15:00:00")]

    store.add_task("过期任务", due_at=datetime(2026, 11, 18, 9, 0), remind_minutes=10)
    missed = _RecorderNotifier()
    remind_daemon._tick(store, _ts(14, 31), catch_up=True, notifier=missed)
    assert missed.sent == [
        ("错过的提醒（1 条）", "- 过期任务（截止 2026-11-18 09:00:00）")
    ]


def test_briefing_pushes_once(tmp_path) -> None:
    store = DeskStore(tmp_path / "desk.db")
    store.add_task("今天开会", due_at=datetime(2026, 11, 18, 15, 0))
    notifier = _RecorderNotifier()

    assert (
        remind_daemon.maybe_send_briefing(
            store, _ts(8, 0), "08:00", notifier=notifier
        )
        is True
    )
    assert len(notifier.sent) == 1
    title, body = notifier.sent[0]
    assert title == "今日简报（2026-11-18）"
    assert "今天开会" in body

    # 同日去重：不再推送
    assert (
        remind_daemon.maybe_send_briefing(store, _ts(9, 0), "08:00", notifier=notifier)
        is False
    )
    assert len(notifier.sent) == 1


def test_push_failure_does_not_break_tick(tmp_path, caplog) -> None:
    class _BoomNotifier:
        def send(self, title: str, body: str) -> bool:
            raise RuntimeError("network down")

    store = DeskStore(tmp_path / "desk.db")
    task = store.add_task(
        "吃药", due_at=datetime(2026, 11, 18, 15, 0), remind_minutes=30
    )[0]

    handled = remind_daemon._tick(store, _ts(14, 30), notifier=_BoomNotifier())

    assert [item.id for item in handled] == [task.id]
    assert store.count_unread_notifications() == 1
    assert "通知推送异常" in caplog.text


def test_main_constructs_notifier_and_pushes(tmp_path, monkeypatch) -> None:
    recorder = _RecorderNotifier()
    calls = []

    def fake_make_notifier(settings=None):
        calls.append(settings)
        return recorder

    monkeypatch.setattr(remind_daemon, "make_notifier", fake_make_notifier)

    db_path = tmp_path / "daemon.db"
    store = DeskStore(db_path)
    store.add_task(
        "过期任务", due_at=datetime(2020, 1, 1, 9, 0), remind_minutes=30
    )

    assert (
        remind_daemon.main(
            ["--db", str(db_path), "--once", "--briefing", "23:59"]
        )
        == 0
    )
    assert len(calls) == 1
    assert any("错过的提醒（1 条）" == title for title, _ in recorder.sent)
