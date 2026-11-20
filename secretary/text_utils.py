"""文本处理小工具：按 UTF-8 字节安全截断。

企业微信文本消息上限按 UTF-8 字节计算，直接按字符截断可能在多字节
字符中间切断，留下乱码。这里提供共享实现，供接收服务器和通知渠道复用。
"""

from __future__ import annotations

DEFAULT_TRUNCATE_SUFFIX = "……"


def truncate_utf8(
    text: str,
    limit: int = 2048,
    suffix: str = DEFAULT_TRUNCATE_SUFFIX,
) -> str:
    """按 UTF-8 字节数截断，绝不切断多字节字符。

    未超过 ``limit`` 时原样返回。超过时保留 ``limit - len(suffix)`` 字节的
    正文，再追加 ``suffix``；如果 ``limit`` 连后缀都放不下，则安全截断后缀
    本身。截断后的结果始终是合法字符串，不会出现半个汉字。
    """

    data = text.encode("utf-8")
    if len(data) <= limit:
        return text

    suffix_bytes = suffix.encode("utf-8")
    if limit <= len(suffix_bytes):
        return suffix_bytes[:limit].decode("utf-8", "ignore")

    # 先按字节切，再用 ignore 丢掉末尾可能被切断的半个字符
    body = data[: limit - len(suffix_bytes)].decode("utf-8", "ignore").rstrip()
    return body + suffix
