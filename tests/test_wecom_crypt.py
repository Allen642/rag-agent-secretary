"""企业微信回调加解密与接收服务器的测试。

加解密用例包含企业微信官方文档《加解密方案说明》的固定测试向量，
用于确保自研实现与官方算法完全一致。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlencode

import pytest

from secretary.config import get_settings
from secretary.notify.wecom_crypt import WeComCrypt

# --------------------------------------------------------------- 官方测试向量
# https://developer.work.weixin.qq.com/document/path/90968
TOKEN = "QDG6eK"
ENCODING_AES_KEY = "jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C"
CORP_ID = "wx5823bf96d3bd56c7"

# 接收消息 XML 解密示例
MSG_SIGNATURE = "477715d11cdb4164915debcba66cb864d751f3e6"
MSG_TIMESTAMP = "1409659813"
MSG_NONCE = "1372623149"
MSG_ENCRYPT = (
    "RypEvHKD8QQKFhvQ6QleEB4J58tiPdvo+rtK1I9qca6aM/wvqnLSV5zEPeusUiX5L5X/"
    "0lWfrf0QADHHhGd3QczcdCUpj911L3vg3W/sYYvuJTs3TUUkSUXxaccAS0qhxchrRYt66"
    "wiSpGLYL42aM6A8dTT+6k4aSknmPj48kzJs8qLjvd4Xgpue06DOdnLxAUHzM6+kDZ+HMZ"
    "fJYuR+LtwGc2hgf5gsijff0ekUNXZiqATP7PF5mZxZ3Izoun1s4zG4LUMnvw2r+KqCKIw"
    "+3IQH03v+BCA9nMELNqbSf6tiWSrXJB3LAVGUcallcrw8V2t9EL4EhzJWrQUax5wLVMNS"
    "0+rUPA3k22Ncx4XXZS9o0MBH27Bo6BpNelZpS+/uh9KsNlY6bHCmJU9p8g7m3fVKn28H"
    "3KDYA5Pl/T8Z1ptDAVe0lXdQ2YoyyH2uyPIGHBZZIs2pDBS8R07+qN+E7Q=="
)
MSG_XML = (
    "<xml><ToUserName><![CDATA[wx5823bf96d3bd56c7]]></ToUserName>"
    "<Encrypt><![CDATA[" + MSG_ENCRYPT + "]]></Encrypt>"
    "<AgentID><![CDATA[218]]></AgentID></xml>"
)

# 验证回调 URL 示例（Sample.py）
ECHO_SIGNATURE = "5c45ff5e21c57e6ad56bac8758b79b1d9ac89fd3"
ECHO_TIMESTAMP = "1409659589"
ECHO_NONCE = "263014780"
ECHO_STR = (
    "P9nAzCzyDtyTWESHep1vC5X9xho/qYX3Zpb4yKa9SKld1DsH3Iyt3tP3zNdtp+4RPc"
    "s8TgAE7OaBO+FZXvnaqQ=="
)
ECHO_PLAINTEXT = "1616140317555161061"

# scripts 不是包，按文件路径加载接收消息服务器模块
_BOT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "wecom_bot.py"
_SPEC = importlib.util.spec_from_file_location("wecom_bot_for_crypt_test", _BOT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
wecom_bot = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = wecom_bot
_SPEC.loader.exec_module(wecom_bot)


def _crypt() -> WeComCrypt:
    return WeComCrypt(TOKEN, ENCODING_AES_KEY, CORP_ID)


# ------------------------------------------------------------------ 加解密往返


@pytest.mark.parametrize(
    "message",
    [
        "hello",
        "你好，企业微信",
        "",
        "多行消息\n第二行\n第三行",
        "x" * 1000,
    ],
)
def test_encrypt_decrypt_roundtrip(message: str) -> None:
    crypt = _crypt()

    assert crypt.decrypt(crypt.encrypt(message)) == message


def test_encrypt_output_is_base64_and_changes_each_time() -> None:
    crypt = _crypt()

    first = crypt.encrypt("同一条消息")
    second = crypt.encrypt("同一条消息")

    # 随机前缀不同，密文不应相同，但都能解回原文
    assert first != second
    assert crypt.decrypt(first) == "同一条消息"
    assert crypt.decrypt(second) == "同一条消息"


# ------------------------------------------------------------------ 签名校验


def test_verify_signature_accepts_official_vector() -> None:
    crypt = _crypt()

    assert crypt.verify_signature(
        MSG_SIGNATURE, MSG_TIMESTAMP, MSG_NONCE, MSG_ENCRYPT
    )


def test_verify_signature_rejects_tampered_values() -> None:
    crypt = _crypt()

    assert not crypt.verify_signature(
        "0" * 40, MSG_TIMESTAMP, MSG_NONCE, MSG_ENCRYPT
    )
    assert not crypt.verify_signature(
        MSG_SIGNATURE, MSG_TIMESTAMP, MSG_NONCE, MSG_ENCRYPT[:-1] + "A"
    )
    assert not crypt.verify_signature(
        MSG_SIGNATURE, MSG_TIMESTAMP, "0", MSG_ENCRYPT
    )


# -------------------------------------------------------------- 官方固定向量


def test_decrypt_official_message_vector() -> None:
    crypt = _crypt()

    plain = crypt.decrypt(MSG_ENCRYPT)

    # 官方文档解出的明文：text 消息，内容 hello
    assert "<Content><![CDATA[hello]]></Content>" in plain
    assert "<ToUserName><![CDATA[wx5823bf96d3bd56c7]]></ToUserName>" in plain
    assert "<AgentID>218</AgentID>" in plain


def test_decrypt_official_echostr_vector() -> None:
    crypt = _crypt()

    assert crypt.decrypt(ECHO_STR) == ECHO_PLAINTEXT


# ------------------------------------------------------------------ 异常输入


def test_invalid_encoding_aes_key_raises() -> None:
    with pytest.raises(ValueError):
        WeComCrypt("token", "too-short", "corp")
    with pytest.raises(ValueError):
        WeComCrypt("token", "!" * 43, "corp")


def test_decrypt_rejects_invalid_base64() -> None:
    with pytest.raises(ValueError):
        _crypt().decrypt("not base64!!!")


def test_decrypt_rejects_broken_ciphertext() -> None:
    with pytest.raises(ValueError):
        # 合法 Base64 但不是 AES 块大小的倍数
        _crypt().decrypt("YWJj")


# ---------------------------------------------------------- 接收消息服务器逻辑


def test_extract_encrypt_from_xml() -> None:
    assert wecom_bot.extract_encrypt(MSG_XML) == MSG_ENCRYPT
    assert wecom_bot.extract_encrypt("<xml></xml>") == ""
    assert wecom_bot.extract_encrypt("not xml") == ""


def test_handle_verify_official_vector() -> None:
    query = urlencode(
        {
            "msg_signature": ECHO_SIGNATURE,
            "timestamp": ECHO_TIMESTAMP,
            "nonce": ECHO_NONCE,
            "echostr": ECHO_STR,
        }
    )
    params = parse_qs(query)

    status, body = wecom_bot.handle_verify(_crypt(), params)

    assert status == 200
    assert body == ECHO_PLAINTEXT


def test_handle_verify_rejects_bad_signature() -> None:
    params = {
        "msg_signature": ["0" * 40],
        "timestamp": [ECHO_TIMESTAMP],
        "nonce": [ECHO_NONCE],
        "echostr": [ECHO_STR],
    }

    status, body = wecom_bot.handle_verify(_crypt(), params)

    assert status == 403
    assert body == ""


def test_handle_message_decrypts_and_logs(caplog) -> None:
    caplog.set_level(logging.INFO, logger="wecom_bot")
    params = {
        "msg_signature": [MSG_SIGNATURE],
        "timestamp": [MSG_TIMESTAMP],
        "nonce": [MSG_NONCE],
    }
    body = MSG_XML.encode("utf-8")

    status, reply = wecom_bot.handle_message(_crypt(), params, body)

    assert status == 200
    assert reply == ""
    assert "hello" in caplog.text


def test_handle_message_rejects_bad_signature() -> None:
    params = {
        "msg_signature": ["0" * 40],
        "timestamp": [MSG_TIMESTAMP],
        "nonce": [MSG_NONCE],
    }

    status, reply = wecom_bot.handle_message(
        _crypt(), params, MSG_XML.encode("utf-8")
    )

    assert status == 403
    assert reply == ""


def test_build_crypt_requires_token_and_aes_key() -> None:
    settings = replace(
        get_settings(),
        wecom_token="",
        wecom_encoding_aes_key="",
    )

    with pytest.raises(RuntimeError) as excinfo:
        wecom_bot.build_crypt(settings)
    assert "WECOM_TOKEN" in str(excinfo.value)
    assert "WECOM_ENCODING_AES_KEY" in str(excinfo.value)


def test_build_crypt_reads_settings() -> None:
    settings = replace(
        get_settings(),
        wecom_token=TOKEN,
        wecom_encoding_aes_key=ENCODING_AES_KEY,
        wecom_corp_id=CORP_ID,
    )

    crypt = wecom_bot.build_crypt(settings)

    assert crypt.decrypt(ECHO_STR) == ECHO_PLAINTEXT


def test_build_crypt_rejects_placeholder_aes_key() -> None:
    settings = replace(
        get_settings(),
        wecom_token=TOKEN,
        wecom_encoding_aes_key="your-encoding-aes-key",
    )

    with pytest.raises(RuntimeError) as excinfo:
        wecom_bot.build_crypt(settings)
    assert "WECOM_ENCODING_AES_KEY" in str(excinfo.value)

