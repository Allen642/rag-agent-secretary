"""企业微信群机器人 Webhook 推送。

群机器人不需要 access_token，也不校验调用方出口 IP，
适合没有固定公网 IP 的家庭宽带环境。

只使用标准库 ``urllib``，不引入额外依赖。发送失败一律记录警告并返回
``False``，由调用方决定是否重试，避免通知异常影响主流程。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from ..text_utils import truncate_utf8
from .base import Notifier
from .wecom import api_error

logger = logging.getLogger(__name__)

# 企业微信群机器人文本消息上限按 UTF-8 字节计算
WECOM_ROBOT_TEXT_LIMIT_BYTES = 2048
REQUEST_TIMEOUT_SECONDS = 10.0
# 企业微信接口的频率限制错误码
FREQUENCY_LIMIT_ERRCODE = 45009


class WeComGroupRobotNotifier(Notifier):
    """通过企业微信群机器人 Webhook 发送文本消息。

    构造参数 ``webhook_url`` 只做首尾去空白，不在构造时校验格式；空串表示
    未配置，发送时会记录警告并返回 ``False``。``timeout`` 是单次 HTTP
    请求的超时时间（秒）。
    """

    def __init__(self, webhook_url: str, *, timeout: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self._webhook_url = (webhook_url or "").strip()
        self._timeout = timeout

    def send(
        self,
        title: str,
        body: str,
        user_id: str | None = None,
    ) -> bool:
        """发送文本消息到群机器人绑定的群。

        群机器人无法定向发送，``user_id`` 参数仅为满足 :class:`Notifier`
        接口而保留，会被忽略。空标题不会在正文前留下多余空行；标题和正文
        都为空时视为“无内容”，直接返回 ``True``。
        """

        if not self._webhook_url:
            logger.warning("企业微信群机器人 Webhook 未配置，已跳过推送")
            return False

        content = "\n".join(part for part in (title, body) if part)
        if not content:
            return True

        content = truncate_utf8(content, WECOM_ROBOT_TEXT_LIMIT_BYTES)
        payload = {"msgtype": "text", "text": {"content": content}}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._webhook_url,
            data=data,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            logger.warning("企业微信群机器人请求失败：%s", exc)
            return False

        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("企业微信群机器人响应不是合法 JSON：%s", exc)
            return False

        error = api_error(result)
        if error is not None:
            errcode = result.get("errcode") if isinstance(result, dict) else None
            if errcode in (FREQUENCY_LIMIT_ERRCODE, str(FREQUENCY_LIMIT_ERRCODE)):
                logger.warning("企业微信群机器人触发频率限制：%s", error)
            else:
                logger.warning("企业微信群机器人消息发送失败：%s", error)
            return False
        return True
