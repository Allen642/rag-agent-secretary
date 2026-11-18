"""通知渠道抽象：daemon 只依赖 ``Notifier``，不关心具体推送实现。"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Notifier(ABC):
    """通知渠道抽象基类。

    ``send`` 是同步调用，成功返回 ``True``，失败返回 ``False``；
    实现方不应向外抛异常，由调用方负责兜底。
    """

    @abstractmethod
    def send(
        self,
        title: str,
        body: str,
        user_id: str | None = None,
    ) -> bool:
        """推送一条通知，成功返回 True。

        ``user_id`` 是可选接收人，只有支持定向发送的渠道才会使用；
        不传时由实现决定默认接收人。
        """


class NoopNotifier(Notifier):
    """未配置任何渠道时的兜底实现。

    这里返回 ``True``：没有配置推送并不是错误，调用方无需反复告警。
    """

    def send(
        self,
        title: str,
        body: str,
        user_id: str | None = None,
    ) -> bool:
        return True
