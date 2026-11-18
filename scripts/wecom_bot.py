"""企业微信接收消息服务器（Slice 6b1）。

运行：
    .\\.venv\\Scripts\\python.exe scripts/wecom_bot.py
    .\\.venv\\Scripts\\python.exe scripts/wecom_bot.py --port 8080

默认监听 ``0.0.0.0:8080``，对外提供 ``/wecom`` 一个端点：

* ``GET /wecom``：企业微信后台「保存」回调 URL 时的验证握手。校验
  ``msg_signature`` 通过后解密 ``echostr``，原样返回明文；
* ``POST /wecom``：接收回调消息。Slice 6b1 只解析 XML 取出 ``Encrypt``、
  校验签名并解密，打印日志后返回空包，暂不触发 SecretaryAssistant。

启动时必须配置 ``WECOM_TOKEN`` 和 ``WECOM_ENCODING_AES_KEY``，缺一直接
报错退出，避免半配置状态对外提供服务。
"""

from __future__ import annotations

import argparse
import logging
import sys
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from secretary.config import Settings, get_settings
from secretary.notify.wecom_crypt import WeComCrypt

logger = logging.getLogger("wecom_bot")

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
ENDPOINT = "/wecom"


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
) -> tuple[int, str]:
    """处理 POST 回调消息，返回 ``(状态码, 响应正文)``。

    Slice 6b1 只解密并打印，不构造被动回复；返回空正文表示不回复。
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

    logger.info("收到企业微信消息：%s", plain)
    return 200, ""


class WeComHandler(BaseHTTPRequestHandler):
    """``/wecom`` 的 HTTP 处理器。"""

    server_version = "WeComBot/0.1"
    # 使用 HTTP/1.1 时必须显式写 Content-Length，否则连接会一直等待
    protocol_version = "HTTP/1.1"
    crypt: WeComCrypt

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
        status, reply = handle_message(self.crypt, params, body)
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

    try:
        crypt = build_crypt(get_settings())
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    WeComHandler.crypt = crypt
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
