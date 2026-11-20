"""企业微信应用消息推送。

只使用标准库 ``urllib``，不引入额外依赖。流程：

1. ``GET /cgi-bin/gettoken`` 用 corpid + corpsecret 换 ``access_token``；
2. ``POST /cgi-bin/message/send`` 以文本消息发给指定成员。

企业微信的 token 有效期是 7200 秒，这里在实例内缓存，并提前 5 分钟刷新，
避免每次提醒都多打一次 token 接口。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from .base import Notifier

logger = logging.getLogger(__name__)

API_BASE = "https://qyapi.weixin.qq.com"
TOKEN_TTL_SECONDS = 7200
# 提前刷新，避免 token 在两次调用之间刚好过期
TOKEN_REFRESH_MARGIN_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 10.0


class WeComNotifier(Notifier):
    """企业微信「应用」消息推送。

    ``agent_id`` 可以是字符串或整数；企业微信接口要求整数，因此纯数字会
    自动转换。``clock`` 仅用于测试 token 缓存，生产环境保持默认即可。
    """

    def __init__(
        self,
        corp_id: str,
        agent_id: str | int,
        secret: str,
        user_id: str = "@all",
        *,
        api_base: str = API_BASE,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._corp_id = (corp_id or "").strip()
        self._secret = (secret or "").strip()
        self._user_id = (user_id or "").strip() or "@all"
        self._agent_id = _coerce_agent_id(agent_id)
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout
        self._clock = clock
        self._access_token: str | None = None
        self._token_expires_at = 0.0

    # -------------------------------------------------------------- public

    def send(
        self,
        title: str,
        body: str,
        user_id: str | None = None,
    ) -> bool:
        """发送文本消息；任何失败都只记日志并返回 False，不抛异常。

        ``user_id`` 用于覆盖构造时的默认接收人。接收消息服务器需要把
        回复发给具体发消息的成员（``FromUserName``），而定时提醒仍可用
        ``WECOM_USER_ID`` 指定的默认接收人。
        """

        token = self._get_access_token()
        if not token:
            return False

        recipient = (user_id or "").strip() or self._user_id
        # 允许只传正文：空标题不留下多余的空行
        content = "\n".join(part for part in (title, body) if part)
        payload = {
            "touser": recipient,
            "msgtype": "text",
            "agentid": self._agent_id,
            "text": {"content": content},
        }
        url = (
            f"{self._api_base}/cgi-bin/message/send?"
            f"access_token={urllib.parse.quote(token)}"
        )
        data = self._request_json(url, payload)
        error = api_error(data)
        if error is not None:
            logger.warning("企业微信消息发送失败：%s", error)
            return False
        return True

    # ------------------------------------------------------------- private

    def _get_access_token(self) -> str | None:
        """返回缓存的 token；临近过期或尚未获取时重新请求。"""

        now = self._clock()
        if self._access_token and now < self._token_expires_at:
            return self._access_token

        query = urllib.parse.urlencode(
            {
                "corpid": self._corp_id,
                "corpsecret": self._secret,
            }
        )
        url = f"{self._api_base}/cgi-bin/gettoken?{query}"
        data = self._request_json(url)

        error = api_error(data)
        if error is not None:
            logger.warning("企业微信 access_token 获取失败：%s", error)
            return None

        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            logger.warning("企业微信 access_token 响应缺少 access_token")
            return None

        expires_in = _positive_int(
            data.get("expires_in") if isinstance(data, dict) else None,
            TOKEN_TTL_SECONDS,
        )
        # 至少缓存到下一次调用之前；expires_in 异常小时也允许立即过期重取
        ttl = max(0, expires_in - TOKEN_REFRESH_MARGIN_SECONDS)
        self._access_token = token
        self._token_expires_at = now + ttl
        return token

    def _request_json(
        self, url: str, payload: dict[str, Any] | None = None
    ) -> Any | None:
        """发起请求并解析 JSON；网络或格式错误统一返回 None。"""

        data: bytes | None = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            logger.warning("企业微信请求失败：%s", exc)
            return None

        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("企业微信响应不是合法 JSON：%s", exc)
            return None


def _coerce_agent_id(value: str | int) -> int | str:
    """企业微信要求 agentid 为整数；非数字配置原样保留，便于服务端报错。"""

    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        return text


def _positive_int(value: Any, default: int) -> int:
    """把接口返回的 expires_in 转成正整数，异常值回落到默认 TTL。"""

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def api_error(data: Any) -> str | None:
    """按企业微信约定检查 ``errcode``；成功返回 None。

    群机器人 Webhook 与应用消息接口共用同一套 ``errcode`` 约定，因此
    提升为公开函数供 :mod:`secretary.notify.wecom_robot` 复用。
    """

    if not isinstance(data, dict):
        return "响应格式异常"
    errcode = data.get("errcode", 0)
    if errcode in (0, "0", None):
        return None
    return f"errcode={errcode} errmsg={data.get('errmsg', '')}"


# 兼容旧调用方：保留原私有名作为别名
_api_error = api_error
