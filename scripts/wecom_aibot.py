"""企业微信「智能机器人」长连接入口。

运行：
    .\\.venv\\Scripts\\python.exe scripts/wecom_aibot.py --check
    .\\.venv\\Scripts\\python.exe scripts/wecom_aibot.py

与 ``scripts/wecom_bot.py``（自建应用 HTTP 回调）不同，这里由本服务主动
连接 ``wss://openws.work.weixin.qq.com``：企业微信沿长连接推送消息，回复也
走同一条连接。因此不需要公网 IP、Tunnel、可信 IP，也不需要建群；代价是同
一个机器人同一时间只能有一个长连接在线。

启动流程：

1. 从 ``.env`` / 环境变量读取 ``WECOM_AIBOT_BOT_ID`` 和
   ``WECOM_AIBOT_SECRET``，缺失时打印明确错误并退出 1；
2. 构造一次共享的 :class:`~secretary.graph.SecretaryAssistant` 和
   :class:`~secretary.conversation_store.ConversationStore`；
3. 创建 SDK 的 ``WSClient``，注册 ``message.text`` 与 ``event.enter_chat``，
   分别交给 :class:`~secretary.aibot_adapter.AibotRunner`；
4. ``client.run()`` 进入事件循环；SDK 内置心跳、断线重连（``-1`` 表示无限）。

``--check`` 只校验配置并退出：配置齐全返回 0，缺失返回 1，便于部署脚本和
systemd 前置检查。日志输出到 stdout，logger 名为 ``wecom_aibot``；启动时只
打印 Bot ID，不打印 Secret。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from secretary.config import Settings, get_settings
from secretary.conversation_store import ConversationStore

logger = logging.getLogger("wecom_aibot")

# 启动前必须存在的配置项；缺失时一次性列全，避免来回改 .env
REQUIRED_SETTINGS: tuple[tuple[str, str], ...] = (
    ("WECOM_AIBOT_BOT_ID", "wecom_aibot_bot_id"),
    ("WECOM_AIBOT_SECRET", "wecom_aibot_secret"),
)


def _configure_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def missing_settings(settings: Settings) -> list[str]:
    """返回缺失的配置项名称（环境变量名），齐全时为空列表。"""

    return [
        env_name
        for env_name, attr_name in REQUIRED_SETTINGS
        if not (getattr(settings, attr_name) or "").strip()
    ]


def make_sender(client, *, new_stream_id=None):
    """构造沿长连接回复的 ``sender``。

    企业微信对 ``enter_chat`` 事件要求用 ``reply_welcome``（``RESPONSE_WELCOME``
    命令）单独回复，普通消息才走 ``reply_stream``；用错命令会导致欢迎语发送
    失败。``new_stream_id`` 仅用于测试注入，默认调用 SDK 生成。
    """

    async def sender(frame: dict, text: str) -> None:
        if frame.get("cmd") == "aibot_event_callback":
            await client.reply_welcome(
                frame,
                {"msgtype": "text", "text": {"content": text}},
            )
            return
        if new_stream_id is None:
            from aibot import generate_req_id

            stream_id = generate_req_id("stream")
        else:
            stream_id = new_stream_id()
        await client.reply_stream(frame, stream_id, text, True)

    return sender


# ---- 连接状态日志 ----


def _on_connected() -> None:
    logger.info("WebSocket 已连接")


def _on_authenticated() -> None:
    logger.info("认证成功，机器人已上线")


def _on_disconnected(reason) -> None:
    logger.warning("连接断开：%s", reason)


def _on_reconnecting(attempt) -> None:
    logger.warning("第 %s 次重连", attempt)


def _on_error(error) -> None:
    logger.error("长连接错误：%s", error)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="企业微信智能机器人长连接（WebSocket 主动连接）"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只校验 WECOM_AIBOT_BOT_ID / WECOM_AIBOT_SECRET 并退出",
    )
    return parser


def _report_missing(missing: list[str]) -> None:
    print(
        "错误：智能机器人长连接缺少配置："
        + "、".join(missing)
        + "。\n"
        "请在 .env 中填写后重试（企业微信机器人详情页 → API 模式 → "
        "使用长连接 → 复制 Bot ID / Secret）：\n"
        "  WECOM_AIBOT_BOT_ID=<Bot ID>\n"
        "  WECOM_AIBOT_SECRET=<Secret>",
        file=sys.stderr,
    )


def run_bot(settings: Settings) -> int:
    """构造助理和客户端并进入长连接事件循环。"""

    # 延迟导入：--check 不需要加载 SDK 和整条 RAG 依赖链
    try:
        from aibot import WSClient, WSClientOptions
    except ImportError as exc:
        print(
            f"错误：未安装企业微信智能机器人 SDK（{exc}）。\n"
            "请先运行：.\\.venv\\Scripts\\python.exe "
            "-m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    from secretary.aibot_adapter import AibotRunner
    from secretary.graph import SecretaryAssistant

    # 整个进程只构造一次：embedding 模型加载很慢
    try:
        assistant = SecretaryAssistant(settings=settings)
    except Exception as exc:  # noqa: BLE001 - 启动阶段统一转成可读报错
        print(
            f"错误：初始化 SecretaryAssistant 失败：{exc}\n"
            "请确认已配置模型 API Key，且依赖与数据目录可用。",
            file=sys.stderr,
        )
        return 1

    conversations = ConversationStore(db_path=settings.conversation_db)
    client = WSClient(
        WSClientOptions(
            bot_id=settings.wecom_aibot_bot_id,
            secret=settings.wecom_aibot_secret,
            # -1：无限重连，适合长期在线的家庭服务
            max_reconnect_attempts=-1,
        )
    )

    runner = AibotRunner(assistant, conversations, make_sender(client))
    client.on("message.text", runner.handle_frame)
    client.on("event.enter_chat", runner.handle_enter_chat)

    # 连接状态日志：认证成功才代表机器人真正上线
    client.on("connected", _on_connected)
    client.on("authenticated", _on_authenticated)
    client.on("disconnected", _on_disconnected)
    client.on("reconnecting", _on_reconnecting)
    client.on("error", _on_error)

    # 只打印 Bot ID；Secret 永不进入日志
    logger.info("智能机器人长连接启动：bot_id=%s", settings.wecom_aibot_bot_id)
    client.run()
    logger.info("智能机器人长连接已退出")
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    _configure_logging()
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    settings = get_settings()

    missing = missing_settings(settings)
    if missing:
        _report_missing(missing)
        return 1

    if args.check:
        print(f"配置检查通过：bot_id={settings.wecom_aibot_bot_id}")
        return 0

    return run_bot(settings)


if __name__ == "__main__":
    raise SystemExit(main())
