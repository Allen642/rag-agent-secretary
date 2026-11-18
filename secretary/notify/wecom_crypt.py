"""企业微信回调消息加解密。

算法见企业微信官方文档《加解密方案说明》：
https://developer.work.weixin.qq.com/document/path/90968

* ``AESKey = Base64_Decode(EncodingAESKey + "=")``，固定 32 字节；
* IV 取 AESKey 前 16 字节，AES-256-CBC；
* 明文结构：``random(16B) + msg_len(4B 大端) + msg + receiveid``；
* 密文为上述明文 AES 加密后再 Base64；
* 签名 ``sha1("".join(sorted([token, timestamp, nonce, encrypt])))``。

只依赖 pycryptodome 的 ``Crypto.Cipher.AES``，不引入官方整包。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import struct

from Crypto.Cipher import AES

_RANDOM_PREFIX_SIZE = 16
_LENGTH_SIZE = 4
_AES_KEY_SIZE = 32
_IV_SIZE = 16
# 企业微信要求 PKCS#7 填充到 32 字节的倍数（不是 AES 的 16 字节）
_PKCS7_BLOCK_SIZE = 32


class WeComCrypt:
    """企业微信回调消息加解密。

    ``corp_id`` 即官方文档中的 ``receiveid``：企业应用回调填 corpid，
    第三方事件回调填 suiteid，个人主体第三方应用填空字符串。
    """

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str) -> None:
        self._token = (token or "").strip()
        self._corp_id = (corp_id or "").encode("utf-8")
        self._key = _decode_aes_key(encoding_aes_key)
        self._iv = self._key[:_IV_SIZE]

    def verify_signature(
        self,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        encrypt: str,
    ) -> bool:
        """校验消息签名；正确返回 True，被篡改或格式不符返回 False。"""

        parts = sorted(
            [
                self._token,
                (timestamp or "").strip(),
                (nonce or "").strip(),
                (encrypt or "").strip(),
            ]
        )
        expected = hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()
        return expected == (msg_signature or "").strip().lower()

    def decrypt(self, encrypt: str) -> str:
        """解密 Base64 密文并返回其中的 ``msg`` 明文。

        密文非法、padding 错误或长度字段越界时抛 :class:`ValueError`。
        明文尾部的 ``receiveid`` 会一并切掉，不做强制校验，便于个人主体
        第三方应用（receiveid 为空）等场景复用。
        """

        try:
            raw = base64.b64decode((encrypt or "").strip(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"企业微信密文不是合法 Base64：{exc}") from exc

        cipher = AES.new(self._key, AES.MODE_CBC, self._iv)
        try:
            decrypted = cipher.decrypt(raw)
        except ValueError as exc:
            raise ValueError(f"企业微信密文长度非法：{exc}") from exc

        plain = _unpad(decrypted)
        header = _RANDOM_PREFIX_SIZE + _LENGTH_SIZE
        if len(plain) < header:
            raise ValueError("企业微信密文解包失败：明文长度不足")
        msg_len = struct.unpack(">I", plain[_RANDOM_PREFIX_SIZE:header])[0]
        msg_start = header
        msg_end = msg_start + msg_len
        if msg_end > len(plain):
            raise ValueError("企业微信密文解包失败：msg_len 越界")
        return plain[msg_start:msg_end].decode("utf-8")

    def encrypt(self, msg: str) -> str:
        """按企业微信规则加密 ``msg``，返回可直接回调的 Base64 密文。"""

        msg_bytes = (msg or "").encode("utf-8")
        plain = (
            os.urandom(_RANDOM_PREFIX_SIZE)
            + struct.pack(">I", len(msg_bytes))
            + msg_bytes
            + self._corp_id
        )
        cipher = AES.new(self._key, AES.MODE_CBC, self._iv)
        return base64.b64encode(cipher.encrypt(_pad(plain))).decode("ascii")


def _decode_aes_key(encoding_aes_key: str) -> bytes:
    """把 43 位 EncodingAESKey 还原成 32 字节 AESKey。"""

    text = (encoding_aes_key or "").strip()
    # EncodingAESKey 是 43 位、去掉 '=' 的 Base64；补足到 4 的倍数再解码
    padded = text + "=" * (-len(text) % 4)
    try:
        key = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"WECOM_ENCODING_AES_KEY 不是合法 Base64：{exc}") from exc
    if len(key) != _AES_KEY_SIZE:
        raise ValueError(
            "WECOM_ENCODING_AES_KEY 解码后应为 "
            f"{_AES_KEY_SIZE} 字节，实际 {len(key)} 字节"
        )
    return key


def _pad(data: bytes) -> bytes:
    """PKCS#7 填充到 32 字节倍数。"""

    pad_len = _PKCS7_BLOCK_SIZE - len(data) % _PKCS7_BLOCK_SIZE
    return data + bytes([pad_len]) * pad_len


def _unpad(data: bytes) -> bytes:
    """去掉 PKCS#7 填充；非法 padding 抛 ``ValueError``。"""

    if not data:
        raise ValueError("企业微信密文解包失败：空明文")
    pad_len = data[-1]
    if not 1 <= pad_len <= _PKCS7_BLOCK_SIZE or pad_len > len(data):
        raise ValueError("企业微信密文解包失败：PKCS#7 padding 非法")
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ValueError("企业微信密文解包失败：PKCS#7 padding 非法")
    return data[:-pad_len]
