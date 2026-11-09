"""从模型文本中提取 JSON 的容错工具。

DeepSeek 思考模式通常能按要求输出纯 JSON，但偶尔会包一层 Markdown
代码块或前后说明；这里统一处理，供 Supervisor 和记忆提取复用。
"""

from __future__ import annotations

import json


def extract_json_object(text: str) -> dict:
    """提取第一个完整 JSON 对象。

    优先直接解析；失败后去掉 Markdown 代码块围栏，再用首尾大括号兜底。
    解析失败会抛出 ValueError，调用方决定是重试还是降级。
    """

    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("模型没有返回合法 JSON") from None
        data = json.loads(cleaned[start : end + 1])

    if not isinstance(data, dict):
        raise TypeError("模型返回的 JSON 顶层必须是对象")
    return data
