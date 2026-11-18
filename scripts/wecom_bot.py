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

启动时必须配置 ``WECOM_TOKEN`` 和 ``WECOM_ENCODING_AES_KEY``，还需要
``DEEPSEEK_API_KEY`` 以及企业微信应用的 corpid/agentid/secret；缺关键
配置时直接报错退出，避免半配置状态对外提供服务。
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from secretary.config import Settings, get_settings
from secretary.notify import NoopNotifier, Notifier, WeComNotifier
from secretary.notify.wecom_crypt import WeComCrypt

if TYPE_CHECKING:
    from secretary.graph import SecretaryAssistant

logger = logging.getLogger("wecom_bot")

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
ENDPOINT = "/wecom"

# 后台线程处理失败时给用户的兜底回复，避免消息石沉大海
REPLY_ERROR_TEXT = "抱歉，秘书处理这条消息时出了点问题，请稍后再试。"
REPLY_THREAD_PREFIX = "wecom-reply"


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """从回调 XML 明文里提取出的最小消息字段。"""

    from_user: str = ""
    msg_type: str = ""
    content: str = ""


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
) -> bool:
    """同步生成回答并主动发给消息发送人；供后台线程和测试调用。

    ``assistant`` 是启动时构造一次的共享实例，这里不负责创建。任何异常
    都在本函数内兜住，避免后台线程把 HTTP 服务拖垮。
    """

    question = message.content.strip()
    if not question:
        logger.info("收到空文本消息，不回复")
        return False

    try:
        answer = assistant.ask(question)
    except Exception:
        logger.exception(
            "处理企业微信消息失败：from=%s content=%s",
            message.from_user,
            question,
        )
        answer = REPLY_ERROR_TEXT

    answer = (answer or "").strip() or REPLY_ERROR_TEXT
    try:
        return notifier.send("", answer, user_id=message.from_user)
    except Exception:
        logger.exception("发送企业微信回复失败：to=%s", message.from_user)
        return False


def dispatch_reply(
    message: IncomingMessage,
    assistant: SecretaryAssistant,
    notifier: Notifier,
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
        args=(message, assistant, notifier),
        name=f"{REPLY_THREAD_PREFIX}-{message.from_user}",
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
) -> tuple[int, str]:
    """处理 POST 回调消息，返回 ``(状态码, 响应正文)``。

    签名校验和解密成功后立即返回空正文（企业微信约定的 ACK）。文本消息
    交给后台线程异步回复，因此不会被被动回复的 5 秒超时截断。传入
    ``assistant`` 和 ``notifier`` 才会真正触发回复；未传时只记录日志，
    便于单元测试和排查。
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
    if assistant is not None and notifier is not None:
        dispatch_reply(message, assistant, notifier)
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
    """构造用于主动回复的通知器；企业微信配置缺失时回退为 Noop。"""

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

    WeComHandler.crypt = crypt
    WeComHandler.assistant = assistant
    WeComHandler.notifier = notifier
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
