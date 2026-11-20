"""企业微信接收消息服务器（Slice 6b3a：文本双向对话）。

运行：
    .\\.venv\\Scripts\\python.exe scripts/wecom_bot.py
    .\\.venv\\Scripts\\python.exe scripts/wecom_bot.py --port 8080

默认监听 ``0.0.0.0:8080``，对外提供 ``/wecom`` 一个端点：

* ``GET /wecom``：企业微信后台「保存」回调 URL 时的验证握手。校验
  ``msg_signature`` 通过后解密 ``echostr``，原样返回明文；
* ``POST /wecom``：接收回调消息。校验签名并解密后解析出
  ``FromUserName``、``MsgType``、``Content``，立即返回空包（表示
  「已收到、不被动回复」），再由后台线程调用共享的
  :class:`SecretaryAssistant` 生成回答，最后用 :class:`WeComNotifier`
  主动调用 ``message/send`` 发给发消息的成员。

企业微信被动回复有 5 秒超时，而 DeepSeek 生成回答通常需要更久，所以
这里统一走「先 ACK、再异步主动回复」。非文本消息暂只记日志，不回复。

为弥补企业微信入口没有 CLI/Web 的会话对象，服务器会用
:class:`ConversationStore` 在进程内保存每个用户最近 20 条消息，并通过
:meth:`SecretaryAssistant.ask_turn` 续接短期上下文；同一用户的多条消息
按到达顺序串行处理，避免上下文互相覆盖。

聊天中发送 ``/model <provider>``（如 ``/model zhipu``）可以全局切换
模型，服务器会同步切换并立即回复结果，不进入后台对话线程。
发送 ``/effort <low|high|max>`` 可以设置 DeepSeek 推理强度（不带参数恢复
默认），同样同步处理并直接回复。
发送 ``/code <需求>`` 会给白名单成员触发一次自动改代码：后台线程在隔离
worktree 里规划、修改并跑 pytest，完成后主动回报分支名和测试结果。白名单
来自环境变量 ``SELF_EDIT_USER_IDS``（逗号分隔），为空时谁都不能触发。

启动时必须配置 ``WECOM_TOKEN`` 和 ``WECOM_ENCODING_AES_KEY``，还需要
``DEEPSEEK_API_KEY`` 以及企业微信应用的 corpid/agentid/secret；缺关键
配置时直接报错退出，避免半配置状态对外提供服务。
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import threading
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from secretary.config import Settings, get_settings
from secretary.notify import (
    NoopNotifier,
    Notifier,
    WeComGroupRobotNotifier,
    WeComNotifier,
)
from secretary.notify.wecom_crypt import WeComCrypt
from secretary.text_utils import truncate_utf8 as _truncate_utf8

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage

    from secretary.graph import SecretaryAssistant

logger = logging.getLogger("wecom_bot")

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
ENDPOINT = "/wecom"

# 后台线程处理失败时给用户的兜底回复，避免消息石沉大海
REPLY_ERROR_TEXT = "抱歉，秘书处理这条消息时出了点问题，请稍后再试。"
REPLY_THREAD_PREFIX = "wecom-reply"

# /code 自动改代码：白名单从 SELF_EDIT_USER_IDS 读取，空集合表示谁都不能触发
ALLOWED_CODE_USERS: frozenset[str] = frozenset(get_settings().self_edit_user_ids)
CODE_USAGE_TEXT = "用法：/code <需求描述>"
CODE_DENIED_TEXT = "你没有触发代码修改的权限。"
CODE_STARTED_TEXT = "收到需求，正在隔离环境里改代码 + 跑测试，稍后回报。"
CODE_ERROR_TEXT = "自动改代码时出了点问题，请查看服务器日志。"
SELF_EDIT_THREAD_PREFIX = "wecom-self-edit"

# 短期对话上下文：每个用户最多保留 20 条，进程内最多保留 256 个用户
CONVERSATION_HISTORY_LIMIT = 20
CONVERSATION_USER_LIMIT = 256
# 企业微信文本消息上限按 UTF-8 字节计算，留出安全余量
WECOM_TEXT_LIMIT_BYTES = 2048
TRUNCATE_SUFFIX = "……"


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """从回调 XML 明文里提取出的最小消息字段。"""

    from_user: str = ""
    msg_type: str = ""
    content: str = ""


class ConversationStore:
    """进程内短期对话存储：按用户隔离历史，并保证同一用户串行处理。

    企业微信没有像 CLI/Web 那样的会话对象，所以这里用一个共享实例挂住
    最近几轮对话。历史只存在内存里，进程重启后清空；如果以后需要跨重启
    保留，可以再替换成 LangGraph 的 SqliteSaver，调用方不需要改。

    * 每个用户最多保留 ``max_messages`` 条消息（按用户裁剪）；
    * 整个进程最多保留 ``max_users`` 个用户，超出按 LRU 淘汰；
    * :meth:`lock_for` 返回该用户专属的锁，避免同一个人连发消息时
      两轮上下文互相覆盖或回复顺序错乱。
    """

    def __init__(
        self,
        *,
        max_messages: int = CONVERSATION_HISTORY_LIMIT,
        max_users: int = CONVERSATION_USER_LIMIT,
    ) -> None:
        self._max_messages = max(1, max_messages)
        self._max_users = max(1, max_users)
        self._history: OrderedDict[str, list[BaseMessage]] = OrderedDict()
        self._locks: dict[str, threading.Lock] = {}
        # 保护 _history/_locks 自身；用户级互斥由 lock_for 返回的锁负责
        self._guard = threading.Lock()

    def lock_for(self, user_id: str) -> threading.Lock:
        """返回某个用户的专属锁；同一用户的消息会按到达顺序串行处理。"""

        with self._guard:
            lock = self._locks.get(user_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[user_id] = lock
            return lock

    def get(self, user_id: str) -> list[BaseMessage]:
        """读取该用户的历史副本；访问也会刷新 LRU 顺序。"""

        with self._guard:
            history = self._history.get(user_id)
            if not history:
                return []
            self._history.move_to_end(user_id)
            return list(history)

    def set(self, user_id: str, history: list[BaseMessage]) -> None:
        """覆盖写入该用户的历史，并裁剪到上限、淘汰最久未使用的用户。"""

        with self._guard:
            trimmed = list(history)[-self._max_messages :]
            if not trimmed:
                self._history.pop(user_id, None)
                return

            self._history[user_id] = trimmed
            self._history.move_to_end(user_id)
            while len(self._history) > self._max_users:
                # 只淘汰 history，不删 _locks。
                # lock_for() 返回锁后、acquire 前若该用户的锁被淘汰，会新建第二把锁，
                # 导致同一用户的消息失去互斥。每个用户一个 Lock 对象很小，保留即可。
                self._history.popitem(last=False)


_MARKDOWN_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MARKDOWN_UNDERLINE = re.compile(r"__(.+?)__")
_MARKDOWN_STRIKE = re.compile(r"~~(.+?)~~")
_MARKDOWN_INLINE_CODE = re.compile(r"`([^`\n]*)`")
_MARKDOWN_FENCE = re.compile(r"```[^\n]*\n?")
_MARKDOWN_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s*")


def _plain_text(text: str) -> str:
    """把 Markdown 精简成企业微信纯文本，避免符号和标题干扰阅读。

    选择纯文本而不是 ``msgtype=markdown``，是因为知识库原文里常有
    ``##`` 等标题，企业微信的 Markdown 子集不保证能渲染。这里去掉
    常见标记，但保留列表项换行和段落结构。
    """

    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _MARKDOWN_FENCE.sub("", text)
    text = _MARKDOWN_INLINE_CODE.sub(r"\1", text)
    text = _MARKDOWN_BOLD.sub(r"\1", text)
    text = _MARKDOWN_UNDERLINE.sub(r"\1", text)
    text = _MARKDOWN_STRIKE.sub(r"\1", text)
    text = _MARKDOWN_HEADING.sub("", text)
    # 折叠三个及以上连续换行，保留正常的段落和列表换行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _ask_with_history(
    assistant: SecretaryAssistant,
    question: str,
    history: list[BaseMessage],
) -> tuple[str, list[BaseMessage]]:
    """调用助理并拿到新历史；兼容只实现了 ``ask`` 的旧调用方/测试替身。"""

    ask_turn = getattr(assistant, "ask_turn", None)
    if callable(ask_turn):
        answer, new_history = ask_turn(question, history=history)
        return answer, list(new_history or [])

    # 兜底：没有 ask_turn 时退化为单轮问答，不保存上下文
    return assistant.ask(question), history


def _configure_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _first(params: dict[str, list[str]], key: str) -> str:
    """取同名查询参数的第一个值；缺失返回空串。"""

    values = params.get(key)
    return values[0].strip() if values else ""


def extract_encrypt(xml_body: bytes | str) -> str:
    """从回调 XML 中取出 ``<Encrypt>`` 文本；格式非法返回空串。"""

    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError:
        return ""
    node = root.find("Encrypt")
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def _child_text(root: ET.Element, tag: str) -> str:
    """取子节点文本并去掉首尾空白；节点缺失返回空串。"""

    node = root.find(tag)
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def parse_message(xml_body: bytes | str) -> IncomingMessage | None:
    """解析解密后的回调 XML，提取发送人、消息类型和文本内容。

    XML 非法时返回 ``None``。企业微信文本消息的 ``FromUserName`` 是发消息
    的成员账号，主动回复时用它作为 ``touser``。
    """

    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError:
        return None
    return IncomingMessage(
        from_user=_child_text(root, "FromUserName"),
        msg_type=_child_text(root, "MsgType").lower(),
        content=_child_text(root, "Content"),
    )


def reply_to_message(
    message: IncomingMessage,
    assistant: SecretaryAssistant,
    notifier: Notifier,
    conversations: ConversationStore | None = None,
) -> bool:
    """同步生成回答并主动发给消息发送人；供后台线程和测试调用。

    ``assistant`` 是启动时构造一次的共享实例，这里不负责创建。任何异常
    都在本函数内兜住，避免后台线程把 HTTP 服务拖垮。

    ``conversations`` 用于同一用户的连续对话；不传时创建一个临时店，
    保证单次调用仍然可用（只是不会跨消息保留上下文）。
    """

    question = message.content.strip()
    if not question:
        logger.info("收到空文本消息，不回复")
        return False

    store = conversations or ConversationStore()

    # 整轮持锁：取历史 -> 生成回答 -> 存历史 -> 发回复，保证同用户按序
    with store.lock_for(message.from_user):
        history = store.get(message.from_user)
        try:
            answer, new_history = _ask_with_history(assistant, question, history)
        except Exception:
            logger.exception(
                "处理企业微信消息失败：from=%s content=%s",
                message.from_user,
                question,
            )
            answer = REPLY_ERROR_TEXT
            new_history = history

        if new_history:
            store.set(message.from_user, new_history)

        # 先清洗 Markdown，再按企业微信文本上限做 UTF-8 安全截断
        answer = _plain_text((answer or "").strip()) or REPLY_ERROR_TEXT
        answer = _truncate_utf8(answer)

        try:
            return notifier.send("", answer, user_id=message.from_user)
        except Exception:
            logger.exception("发送企业微信回复失败：to=%s", message.from_user)
            return False


def dispatch_reply(
    message: IncomingMessage,
    assistant: SecretaryAssistant,
    notifier: Notifier,
    conversations: ConversationStore | None = None,
) -> threading.Thread | None:
    """为文本消息启动后台回复线程；不满足条件时返回 ``None``。"""

    if not message.from_user:
        logger.warning("消息缺少 FromUserName，无法主动回复")
        return None
    if message.msg_type != "text":
        logger.info("暂不支持 %s 类型消息，已忽略", message.msg_type or "未知")
        return None
    if not message.content.strip():
        logger.info("收到空文本消息，已忽略")
        return None

    thread = threading.Thread(
        target=reply_to_message,
        args=(message, assistant, notifier, conversations),
        name=f"{REPLY_THREAD_PREFIX}-{message.from_user}",
        daemon=True,
    )
    thread.start()
    return thread


def _run_self_edit(requirement: str, user_id: str, notifier: Notifier) -> None:
    """后台线程入口：跑 mini coding agent，并把结果主动发给触发者。"""

    # scripts 不是包，运行时把同目录加进 sys.path 再导入 self_edit
    scripts_dir = str(Path(__file__).resolve().parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    try:
        from self_edit import self_edit

        result = self_edit(requirement)
    except Exception:  # noqa: BLE001 - 后台线程不能把异常抛给 HTTP 服务
        logger.exception("自动改代码失败：user=%s", user_id)
        result = CODE_ERROR_TEXT

    try:
        notifier.send("", result, user_id=user_id)
    except Exception:  # noqa: BLE001 - 发送失败只记日志
        logger.exception("发送自动改代码结果失败：to=%s", user_id)


def _dispatch_self_edit(
    requirement: str,
    user_id: str,
    notifier: Notifier,
) -> threading.Thread:
    """把自动改代码放到后台线程，避免阻塞企业微信回调。"""

    thread = threading.Thread(
        target=_run_self_edit,
        args=(requirement, user_id, notifier),
        name=f"{SELF_EDIT_THREAD_PREFIX}-{user_id}",
        daemon=True,
    )
    thread.start()
    return thread


def handle_verify(
    crypt: WeComCrypt,
    params: dict[str, list[str]],
) -> tuple[int, str]:
    """处理 GET 验证握手，返回 ``(状态码, 响应正文)``。

    ``params`` 来自 :func:`urllib.parse.parse_qs`，已经做过 URL 解码。
    """

    msg_signature = _first(params, "msg_signature")
    timestamp = _first(params, "timestamp")
    nonce = _first(params, "nonce")
    echostr = _first(params, "echostr")
    if not (msg_signature and timestamp and nonce and echostr):
        return 400, ""
    if not crypt.verify_signature(msg_signature, timestamp, nonce, echostr):
        logger.warning("GET /wecom 签名校验失败")
        return 403, ""
    try:
        return 200, crypt.decrypt(echostr)
    except ValueError as exc:
        logger.warning("GET /wecom echostr 解密失败：%s", exc)
        return 400, ""


def handle_message(
    crypt: WeComCrypt,
    params: dict[str, list[str]],
    body: bytes,
    *,
    assistant: SecretaryAssistant | None = None,
    notifier: Notifier | None = None,
    conversations: ConversationStore | None = None,
) -> tuple[int, str]:
    """处理 POST 回调消息，返回 ``(状态码, 响应正文)``。

    签名校验和解密成功后立即返回空正文（企业微信约定的 ACK）。文本消息
    交给后台线程异步回复，因此不会被被动回复的 5 秒超时截断。传入
    ``assistant`` 和 ``notifier`` 才会真正触发回复；未传时只记录日志，
    便于单元测试和排查。``conversations`` 由服务器共享，用于续接同一用户
    的短期上下文。
    """

    encrypt = extract_encrypt(body)
    if not encrypt:
        logger.warning("POST /wecom 请求体中没有 <Encrypt>")
        return 400, ""

    msg_signature = _first(params, "msg_signature")
    timestamp = _first(params, "timestamp")
    nonce = _first(params, "nonce")
    if not (msg_signature and timestamp and nonce):
        logger.warning("POST /wecom 缺少签名参数")
        return 403, ""
    if not crypt.verify_signature(msg_signature, timestamp, nonce, encrypt):
        logger.warning("POST /wecom 签名校验失败")
        return 403, ""

    try:
        plain = crypt.decrypt(encrypt)
    except ValueError as exc:
        logger.warning("POST /wecom 消息解密失败：%s", exc)
        return 400, ""

    message = parse_message(plain)
    if message is None:
        logger.warning("POST /wecom 明文 XML 解析失败：%s", plain)
        return 200, ""

    logger.info(
        "收到企业微信消息：from=%s type=%s content=%s",
        message.from_user,
        message.msg_type,
        message.content,
    )
    # /model 是本地即时命令：同步切换并直接回复，不走后台线程和对话框
    content = message.content.strip()
    if content.startswith("/model") and assistant is not None and notifier is not None:
        parts = content.split()
        provider = parts[1] if len(parts) > 1 else ""
        try:
            result = assistant.switch_model(provider or None)
        except Exception as exc:
            result = f"切换失败：{exc}"
        notifier.send("", result, user_id=message.from_user)
        return 200, ""

    # /effort 同样走同步处理：切换推理强度只影响后续对话，不需要排队
    if content.startswith("/effort") and assistant is not None and notifier is not None:
        parts = content.split()
        effort = parts[1] if len(parts) > 1 else ""
        try:
            result = assistant.switch_effort(effort or None)
        except Exception as exc:
            result = f"设置失败：{exc}"
        notifier.send("", result, user_id=message.from_user)
        return 200, ""

    # /code 触发自动改代码：先白名单校验，再丢后台线程，立即 ACK
    if content.startswith("/code") and assistant is not None and notifier is not None:
        requirement = content[len("/code") :].strip()
        if not requirement:
            notifier.send("", CODE_USAGE_TEXT, user_id=message.from_user)
            return 200, ""
        if message.from_user not in ALLOWED_CODE_USERS:
            logger.warning("/code 被拒绝：from=%s", message.from_user)
            notifier.send("", CODE_DENIED_TEXT, user_id=message.from_user)
            return 200, ""
        _dispatch_self_edit(requirement, message.from_user, notifier)
        notifier.send("", CODE_STARTED_TEXT, user_id=message.from_user)
        return 200, ""

    if assistant is not None and notifier is not None:
        if conversations is None:
            dispatch_reply(message, assistant, notifier)
        else:
            dispatch_reply(message, assistant, notifier, conversations)
    else:
        logger.info("未配置 SecretaryAssistant/通知器，仅记录消息")
    return 200, ""


class WeComHandler(BaseHTTPRequestHandler):
    """``/wecom`` 的 HTTP 处理器。"""

    server_version = "WeComBot/0.2"
    # 使用 HTTP/1.1 时必须显式写 Content-Length，否则连接会一直等待
    protocol_version = "HTTP/1.1"
    crypt: WeComCrypt
    assistant: SecretaryAssistant | None = None
    notifier: Notifier | None = None
    conversations: ConversationStore | None = None

    def do_GET(self) -> None:
        if not self._is_endpoint():
            self._send(404, "")
            return
        params = parse_qs(urlsplit(self.path).query)
        status, body = handle_verify(self.crypt, params)
        self._send(status, body)

    def do_POST(self) -> None:
        if not self._is_endpoint():
            self._send(404, "")
            return
        length = self._content_length()
        body = self.rfile.read(length) if length else b""
        params = parse_qs(urlsplit(self.path).query)
        status, reply = handle_message(
            self.crypt,
            params,
            body,
            assistant=self.assistant,
            notifier=self.notifier,
            conversations=self.conversations,
        )
        self._send(status, reply)

    def log_message(self, format_: str, *args: Any) -> None:
        """把 http.server 的访问日志交给 logging。"""

        logger.info("%s - %s", self.address_string(), format_ % args)

    def _is_endpoint(self) -> bool:
        return urlsplit(self.path).path.rstrip("/") == ENDPOINT

    def _content_length(self) -> int:
        raw = self.headers.get("Content-Length", "0")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 0
        return max(0, value)

    def _send(self, status: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)


def build_crypt(settings: Settings | None = None) -> WeComCrypt:
    """按配置构造 :class:`WeComCrypt`；缺少必要配置时抛 ``RuntimeError``。"""

    settings = settings or get_settings()
    missing = [
        name
        for name, value in (
            ("WECOM_TOKEN", settings.wecom_token),
            ("WECOM_ENCODING_AES_KEY", settings.wecom_encoding_aes_key),
        )
        if not (value or "").strip()
    ]
    if missing:
        raise RuntimeError(
            "企业微信接收消息服务器缺少配置："
            + "、".join(missing)
            + "。请在 .env 中填写后重试。"
        )
    try:
        return WeComCrypt(
            token=settings.wecom_token,
            encoding_aes_key=settings.wecom_encoding_aes_key,
            corp_id=settings.wecom_corp_id,
        )
    except ValueError as exc:
        raise RuntimeError(
            f"企业微信接收消息服务器配置错误：{exc}"
        ) from exc


def build_assistant(settings: Settings) -> SecretaryAssistant:
    """构造一次共享的 :class:`SecretaryAssistant`（加载 embedding 很重）。

    导入放在函数内，避免只跑加解密单元测试时也加载整条 RAG 依赖链。
    """

    from secretary.graph import SecretaryAssistant

    return SecretaryAssistant(settings=settings)


def build_reply_notifier(settings: Settings) -> Notifier:
    """构造用于主动回复的通知器；企业微信配置缺失时回退为 Noop。

    群机器人优先级最高：只要配置了 ``WECOM_ROBOT_WEBHOOK``，回复就发到
    对应的群，不再看 ``NOTIFY_CHANNEL`` 和应用消息配置。群机器人不校验
    出口 IP，适合家庭宽带环境。
    """

    webhook = (settings.wecom_robot_webhook or "").strip()
    if webhook:
        logger.info("主动回复通道：企业微信群机器人")
        return WeComGroupRobotNotifier(webhook)

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
            "企业微信主动回复缺少配置（%s），将不会发送回复",
            "、".join(missing),
        )
        return NoopNotifier()

    return WeComNotifier(
        corp_id=settings.wecom_corp_id,
        agent_id=settings.wecom_agent_id,
        secret=settings.wecom_secret,
        user_id=settings.wecom_user_id,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="企业微信接收消息服务器（GET 验证 + POST 解密）"
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"监听地址，默认 {DEFAULT_HOST}",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"监听端口，默认 {DEFAULT_PORT}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    _configure_logging()
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    settings = get_settings()

    try:
        crypt = build_crypt(settings)
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    # 整个进程只构造一次：embedding 模型加载很慢，不能每个请求 new 一个
    try:
        assistant = build_assistant(settings)
    except Exception as exc:  # noqa: BLE001 - 启动阶段统一转成可读报错
        print(
            f"错误：初始化 SecretaryAssistant 失败：{exc}\n"
            "请确认已配置 DEEPSEEK_API_KEY，且依赖与数据目录可用。",
            file=sys.stderr,
        )
        return 1
    notifier = build_reply_notifier(settings)
    if isinstance(notifier, WeComGroupRobotNotifier):
        channel_name = "企业微信群机器人"
    elif isinstance(notifier, WeComNotifier):
        channel_name = "企业微信应用消息"
    else:
        channel_name = "不推送"
    logger.info("主动回复通道：%s", channel_name)
    conversations = ConversationStore()

    WeComHandler.crypt = crypt
    WeComHandler.assistant = assistant
    WeComHandler.notifier = notifier
    WeComHandler.conversations = conversations
    try:
        server = ThreadingHTTPServer((args.host, args.port), WeComHandler)
    except OSError as exc:
        print(f"错误：无法监听 {args.host}:{args.port}：{exc}", file=sys.stderr)
        return 1

    logger.info(
        "企业微信接收消息服务器已启动：http://%s:%d%s",
        args.host,
        args.port,
        ENDPOINT,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("企业微信接收消息服务器已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
