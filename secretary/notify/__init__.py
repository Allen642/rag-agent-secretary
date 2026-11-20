"""通知推送渠道。

支持企业微信「应用」消息和「群机器人」Webhook，通过 ``make_notifier``
按配置选择实现：

* ``NOTIFY_CHANNEL=none``（默认）→ :class:`NoopNotifier`，不推送；
* ``NOTIFY_CHANNEL=wecom`` 且企业微信应用配置齐全 → :class:`WeComNotifier`；
* ``NOTIFY_CHANNEL=wecom_robot`` 且 ``WECOM_ROBOT_WEBHOOK`` 非空
  → :class:`WeComGroupRobotNotifier`；
* 配置不完整或渠道名未知 → 记一条警告并回退到 :class:`NoopNotifier`，
  保证 daemon 在任何情况下都能继续跑。
"""

from __future__ import annotations

import logging

from ..config import Settings, get_settings
from .base import NoopNotifier, Notifier
from .wecom import WeComNotifier
from .wecom_robot import WeComGroupRobotNotifier

logger = logging.getLogger(__name__)

__all__ = [
    "NoopNotifier",
    "Notifier",
    "WeComGroupRobotNotifier",
    "WeComNotifier",
    "make_notifier",
]


def make_notifier(settings: Settings | None = None) -> Notifier:
    """按 ``notify_channel`` 构造通知器；未配置时返回 Noop。"""

    settings = settings or get_settings()
    channel = (settings.notify_channel or "none").strip().lower()

    if channel in ("", "none"):
        return NoopNotifier()

    if channel == "wecom_robot":
        webhook = (settings.wecom_robot_webhook or "").strip()
        if not webhook:
            logger.warning(
                "企业微信群机器人 Webhook 未配置（WECOM_ROBOT_WEBHOOK），已回退为不推送"
            )
            return NoopNotifier()
        return WeComGroupRobotNotifier(webhook)

    if channel != "wecom":
        logger.warning("未知通知渠道 %r，已回退为不推送", settings.notify_channel)
        return NoopNotifier()

    missing = [
        name
        for name, value in (
            ("WECOM_CORP_ID", settings.wecom_corp_id),
            ("WECOM_AGENT_ID", settings.wecom_agent_id),
            ("WECOM_SECRET", settings.wecom_secret),
        )
        if not (value or "").strip()
    ]
    if missing:
        logger.warning(
            "企业微信配置不完整（缺少 %s），已回退为不推送",
            "、".join(missing),
        )
        return NoopNotifier()

    return WeComNotifier(
        corp_id=settings.wecom_corp_id,
        agent_id=settings.wecom_agent_id,
        secret=settings.wecom_secret,
        user_id=settings.wecom_user_id,
    )
