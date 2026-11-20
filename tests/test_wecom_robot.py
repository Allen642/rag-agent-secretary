"""企业微信群机器人 Webhook 通知的测试（全部 mock urllib，不访问网络）。"""

from __future__ import annotations

import json
from typing import Self
from urllib.error import URLError

import pytest

import secretary.notify.wecom_robot as wecom_robot_module
from secretary.notify.wecom_robot import WeComGroupRobotNotifier

WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key"


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
    """把 wecom_robot 模块里的 urlopen 替换成可编排的 fake。"""

    def install(*responses: dict | bytes | Exception) -> _Recorder:
        recorder = _Recorder(list(responses))
        monkeypatch.setattr(
            wecom_robot_module.urllib.request, "urlopen", recorder
        )
        return recorder

    return install


def test_send_posts_text_payload(install_urlopen) -> None:
    recorder = install_urlopen({"errcode": 0, "errmsg": "ok"})

    assert WeComGroupRobotNotifier(WEBHOOK).send("交房租", "截止：2026-11-20 09:00") is True
    assert len(recorder.requests) == 1

    request, timeout = recorder.requests[0]
    assert request.full_url == WEBHOOK  # type: ignore[attr-defined]
    assert request.get_method() == "POST"  # type: ignore[attr-defined]
    assert request.get_header("Content-type") == "application/json"  # type: ignore[attr-defined]
    assert timeout == wecom_robot_module.REQUEST_TIMEOUT_SECONDS

    payload = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
    assert payload == {
        "msgtype": "text",
        "text": {"content": "交房租\n截止：2026-11-20 09:00"},
    }


def test_frequency_limit_returns_false_and_logs(install_urlopen, caplog) -> None:
    install_urlopen({"errcode": 45009, "errmsg": "api freq out of limit"})

    assert WeComGroupRobotNotifier(WEBHOOK).send("标题", "正文") is False
    assert "频率限制" in caplog.text
    assert "45009" in caplog.text
    assert "api freq out of limit" in caplog.text


def test_network_error_returns_false(install_urlopen, caplog) -> None:
    recorder = install_urlopen(URLError("boom"))

    assert WeComGroupRobotNotifier(WEBHOOK).send("标题", "正文") is False
    assert len(recorder.requests) == 1
    assert "请求失败" in caplog.text


def test_non_json_response_returns_false(install_urlopen, caplog) -> None:
    install_urlopen(b"not json")

    assert WeComGroupRobotNotifier(WEBHOOK).send("标题", "正文") is False
    assert "不是合法 JSON" in caplog.text


def test_overlong_content_is_truncated_in_request(install_urlopen) -> None:
    recorder = install_urlopen({"errcode": 0, "errmsg": "ok"})

    assert WeComGroupRobotNotifier(WEBHOOK).send("", "中" * 3000) is True

    request, _ = recorder.requests[0]
    payload = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
    content = payload["text"]["content"]
    assert len(content.encode("utf-8")) <= 2048
    assert content.endswith("……")
    assert "\ufffd" not in content


def test_empty_webhook_returns_false_without_request(install_urlopen, caplog) -> None:
    recorder = install_urlopen()

    assert WeComGroupRobotNotifier("  ").send("标题", "正文") is False
    assert recorder.requests == []
    assert "未配置" in caplog.text


def test_invalid_webhook_url_returns_false_without_raising(
    install_urlopen, caplog
) -> None:
    recorder = install_urlopen()

    assert WeComGroupRobotNotifier("not-a-url").send("标题", "正文") is False

    assert recorder.requests == []
    assert "Webhook 地址无效" in caplog.text


def test_user_id_is_ignored_but_accepted(install_urlopen) -> None:
    recorder = install_urlopen({"errcode": 0, "errmsg": "ok"})

    assert (
        WeComGroupRobotNotifier(WEBHOOK).send("标题", "正文", user_id="zhangsan")
        is True
    )

    payload = json.loads(recorder.requests[0][0].data.decode("utf-8"))  # type: ignore[attr-defined]
    assert payload == {"msgtype": "text", "text": {"content": "标题\n正文"}}
