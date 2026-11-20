"""``scripts/wecom_aibot.py`` 启动前配置校验的测试。

只覆盖 ``--check`` 分支：不构造助理、不联网，避免测试依赖真实 Bot 凭证。
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

from secretary.config import get_settings

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "wecom_aibot.py"
_SPEC = importlib.util.spec_from_file_location("wecom_aibot_for_test", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
wecom_aibot = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = wecom_aibot
_SPEC.loader.exec_module(wecom_aibot)


def _fixed_settings(*, bot_id: str, secret: str):
    """在真实配置基础上替换智能机器人两项，避免读写 .env。"""

    return replace(
        get_settings(),
        wecom_aibot_bot_id=bot_id,
        wecom_aibot_secret=secret,
    )


def test_check_fails_with_clear_message_when_missing(monkeypatch, capsys) -> None:
    settings = _fixed_settings(bot_id="", secret="")
    monkeypatch.setattr(wecom_aibot, "get_settings", lambda: settings)

    assert wecom_aibot.main(["--check"]) == 1

    err = capsys.readouterr().err
    assert "WECOM_AIBOT_BOT_ID" in err
    assert "WECOM_AIBOT_SECRET" in err


def test_check_reports_only_missing_item(monkeypatch, capsys) -> None:
    settings = _fixed_settings(bot_id="bot-123", secret="")
    monkeypatch.setattr(wecom_aibot, "get_settings", lambda: settings)

    assert wecom_aibot.main(["--check"]) == 1

    err = capsys.readouterr().err
    assert "WECOM_AIBOT_SECRET" in err
    assert "WECOM_AIBOT_BOT_ID、" not in err


def test_check_passes_when_configured(monkeypatch, capsys) -> None:
    settings = _fixed_settings(bot_id="bot-123", secret="secret-xyz")
    monkeypatch.setattr(wecom_aibot, "get_settings", lambda: settings)

    assert wecom_aibot.main(["--check"]) == 0

    out = capsys.readouterr().out
    assert "配置检查通过" in out
    assert "bot-123" in out
    # Secret 不进入任何输出
    assert "secret-xyz" not in out
