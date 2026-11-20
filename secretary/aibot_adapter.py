"""企业微信「智能机器人」长连接帧适配层。

智能机器人走 WebSocket 长连接（``wss://openws.work.weixin.qq.com``），
企业微信通过连接推送 JSON 帧，开发者再沿同一条连接回复。相比自建应用的
HTTP 回调，这里不需要公网 IP、不需要加解密、也不需要建群。

本模块只负责“把 SDK 收到的帧翻译成一轮对话”：

* :func:`parse_text_frame` 从 ``aibot_msg_callback`` 帧中提取文本消息，
  非文本消息、事件帧和缺字段的帧统一返回 ``None``；
* :class:`AibotRunner` 按 ``msgid`` 去重（SDK 断线重连后可能重推），
  用 ``ConversationStore`` 保证同一用户串行，并调用现有的
  :class:`~secretary.graph.SecretaryAssistant` 生成回答；
* 生成回答放到 ``asyncio.to_thread`` 里执行，避免阻塞事件循环——
  长连接的心跳必须持续发送，否则会被服务端断开。

回复统一截断到 ``reply_limit_bytes``（默认 32 KB，企业微信智能机器人
流式文本上限）。``/model``、``/effort`` 是本地即时命令，不走对话；
``/code`` 在长连接模式暂不支持。所有异常都在本模块内兜住，不会中断
连接：失败时给用户一句兜底文案，或只记日志。
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .text_utils import truncate_utf8

if TYPE_CHECKING:
    from .conversation_store import ConversationStore
    from .graph import SecretaryAssistant

logger = logging.getLogger("aibot_adapter")

# 只处理消息回调帧；aibot_event_callback 由 handle_enter_chat 单独处理
MSG_CALLBACK_CMD = "aibot_msg_callback"
TEXT_MSG_TYPE = "text"

# 企业微信智能机器人流式文本上限按 UTF-8 字节计算
DEFAULT_REPLY_LIMIT_BYTES = 32768
# 去重窗口：保存最近处理过的 msgid 数量
DEFAULT_MAX_DEDUP = 512

# 异常 / 空回答时给用户的兜底文案
REPLY_ERROR_TEXT = "抱歉，秘书处理这条消息时出了点问题，请稍后再试。"
# 进入会话事件的欢迎语，必须 5 秒内发出
WELCOME_TEXT = "你好，我是你的私人秘书。直接发消息就能开始，我会尽快回复。"
# 长连接模式不提供自动改代码能力
CODE_UNSUPPORTED_TEXT = "长连接模式暂不支持 /code。"

# sender 的签名：``async def sender(frame, text) -> None``
Sender = Callable[[dict, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class IncomingAibotMessage:
    """从智能机器人文本帧里提取出的最小消息字段。

    ``frame`` 保留原始帧，回复时需要用它的 ``headers.req_id`` 透传。
    """

    user_id: str
    text: str
    msgid: str
    chat_type: str
    chat_id: str
    frame: dict


def _as_dict(value: Any) -> dict:
    """非 dict 一律视为空 dict，避免脏帧触发 ``AttributeError``。"""

    return value if isinstance(value, dict) else {}


def _as_text(value: Any) -> str:
    """只接受字符串，并去掉首尾空白。"""

    return value.strip() if isinstance(value, str) else ""


def parse_text_frame(frame: dict) -> IncomingAibotMessage | None:
    """解析 ``aibot_msg_callback`` 帧中的文本消息。

    只处理 ``body.msgtype == "text"`` 且正文非空的消息；事件帧、图片 /
    文件 / 语音等消息、字段缺失或类型不对的帧都返回 ``None``。
    ``user_id`` 优先取 ``body.from.userid``，单聊缺该字段时退回
    ``body.chatid``；群聊时 ``chat_id`` 取 ``body.chatid``。
    """

    if not isinstance(frame, dict):
        return None
    if frame.get("cmd") != MSG_CALLBACK_CMD:
        return None

    body = _as_dict(frame.get("body"))
    if body.get("msgtype") != TEXT_MSG_TYPE:
        return None

    text = _as_text(_as_dict(body.get("text")).get("content"))
    if not text:
        return None

    chat_id = _as_text(body.get("chatid"))
    user_id = _as_text(_as_dict(body.get("from")).get("userid")) or chat_id
    return IncomingAibotMessage(
        user_id=user_id,
        text=text,
        msgid=_as_text(body.get("msgid")),
        chat_type=_as_text(body.get("chattype")),
        chat_id=chat_id,
        frame=frame,
    )


class AibotRunner:
    """把长连接文本帧接到现有助理上的处理器。

    * 按 ``msgid`` 去重，窗口大小 ``max_dedup``；
    * 同一 ``user_id`` 通过 ``ConversationStore.lock_for`` 串行处理；
    * 回答经 ``truncate_utf8`` 截断后交给 ``sender``，发送失败只记日志。
    """

    def __init__(
        self,
        assistant: "SecretaryAssistant",
        conversations: "ConversationStore",
        sender: Sender,
        *,
        max_dedup: int = DEFAULT_MAX_DEDUP,
        reply_limit_bytes: int = DEFAULT_REPLY_LIMIT_BYTES,
    ) -> None:
        self._assistant = assistant
        self._conversations = conversations
        self._sender = sender
        self._max_dedup = max(1, int(max_dedup))
        self._reply_limit_bytes = max(1, int(reply_limit_bytes))
        self._seen: OrderedDict[str, None] = OrderedDict()

    async def handle_frame(self, frame: dict) -> None:
        """处理一帧消息：解析 → 去重 → 生成 → 发送。"""

        message = parse_text_frame(frame)
        if message is None:
            logger.debug("忽略非文本帧或事件帧")
            return

        if self._is_duplicate(message.msgid):
            logger.info("忽略重复消息：msgid=%s", message.msgid)
            return

        try:
            # 模型调用是同步阻塞的，必须丢到线程里跑，心跳才不会被卡住
            answer = await asyncio.to_thread(self._run_turn, message)
        except Exception:  # noqa: BLE001 - 后台任务不能把异常抛给 SDK
            logger.exception(
                "处理智能机器人消息失败：user=%s msgid=%s",
                message.user_id,
                message.msgid,
            )
            answer = REPLY_ERROR_TEXT

        answer = truncate_utf8(
            (answer or "").strip() or REPLY_ERROR_TEXT,
            self._reply_limit_bytes,
        )
        try:
            await self._sender(frame, answer)
        except Exception:  # noqa: BLE001 - 发送失败只记日志，保持连接
            logger.exception(
                "发送智能机器人回复失败：user=%s msgid=%s",
                message.user_id,
                message.msgid,
            )

    async def handle_enter_chat(self, frame: dict) -> None:
        """回复 ``enter_chat`` 事件：企业微信要求 5 秒内发出欢迎语。"""

        try:
            await self._sender(frame, WELCOME_TEXT)
        except Exception:  # noqa: BLE001 - 欢迎语失败不影响后续对话
            logger.exception("发送进入会话欢迎语失败")

    # ---- 内部 ----

    def _is_duplicate(self, msgid: str) -> bool:
        """记录并判断 msgid 是否已处理；空 msgid 不参与去重。"""

        if not msgid:
            return False
        if msgid in self._seen:
            # 命中也刷新顺序，避免热点消息被过早挤出窗口
            self._seen.move_to_end(msgid)
            return True
        self._seen[msgid] = None
        while len(self._seen) > self._max_dedup:
            self._seen.popitem(last=False)
        return False

    def _run_turn(self, message: IncomingAibotMessage) -> str:
        """在工作线程里执行一轮对话，返回待发送的文本。

        该方法同步执行，任何异常都转成用户可读文案，不向上抛。
        """

        text = message.text.strip()
        if text.startswith("/model"):
            return self._handle_model_command(text)
        if text.startswith("/effort"):
            return self._handle_effort_command(text)
        if text.startswith("/code"):
            return CODE_UNSUPPORTED_TEXT

        with self._conversations.lock_for(message.user_id):
            history = self._conversations.get(message.user_id)
            try:
                answer, new_history = self._assistant.ask_turn(
                    text, history=history
                )
            except Exception:  # noqa: BLE001 - 单轮失败不该断连接
                logger.exception(
                    "助理处理消息失败：user=%s msgid=%s",
                    message.user_id,
                    message.msgid,
                )
                return REPLY_ERROR_TEXT

            self._conversations.set(message.user_id, list(new_history or []))
            return (answer or "").strip() or REPLY_ERROR_TEXT

    def _handle_model_command(self, text: str) -> str:
        """/model 是全局即时命令，直接切换模型，不进入对话。"""

        parts = text.split()
        provider = parts[1] if len(parts) > 1 else ""
        try:
            return self._assistant.switch_model(provider or None)
        except Exception as exc:  # noqa: BLE001 - 转成可读提示
            logger.warning("切换模型失败：%s", exc)
            return f"切换失败：{exc}"

    def _handle_effort_command(self, text: str) -> str:
        """/effort 同样即时生效，只影响后续对话。"""

        parts = text.split()
        effort = parts[1] if len(parts) > 1 else ""
        try:
            return self._assistant.switch_effort(effort or None)
        except Exception as exc:  # noqa: BLE001 - 转成可读提示
            logger.warning("设置推理强度失败：%s", exc)
            return f"设置失败：{exc}"
