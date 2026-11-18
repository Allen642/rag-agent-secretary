"""长期记忆管理员。

每轮对话结束（Supervisor 选择 FINISH）后运行一次：
  1. 只读取用户最近的发言，避免把助手生成的内容写进记忆；
  2. 让 DeepSeek 判断是否有跨会话价值，并输出 JSON；
  3. 写入 ChromaDB，使用内容哈希作为稳定 ID，重复内容自动覆盖而不是堆积。

记忆写入失败不会影响本轮回答，只会在状态里标记错误。
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, ValidationError

from .json_utils import extract_json_object
from .message_utils import message_text
from .prompts import MEMORY_CURATOR_SYSTEM_PROMPT
from .rag import ChromaKnowledgeBase
from .state import SecretaryState

logger = logging.getLogger(__name__)

MemoryKind = Literal["fact", "preference", "goal", "constraint", "plan", "note"]

_SENSITIVE_PATTERNS = re.compile(
    r"(api\s*key|apikey|password\s*[=:：]|密码\s*[=:：]|身份证号|银行卡号|"
    r"信用卡号|sk-[A-Za-z0-9]{16,})",
    re.IGNORECASE,
)
_EXPLICIT_MEMORY_HINTS = ("记住", "记一下", "保存", "别忘了", "以后按", "长期记忆")
# BGE 余弦距离下，非常相近的改写通常 <0.1；阈值取小一些，避免误合并不同事实
_SEMANTIC_DEDUP_DISTANCE = 0.12


class MemoryItem(BaseModel):
    """单条长期记忆。"""

    content: str = Field(min_length=1, max_length=500)
    topic: str = Field(default="个人", max_length=50)
    kind: MemoryKind = "fact"


class MemoryDecision(BaseModel):
    """模型输出的记忆提取结果。"""

    should_store: bool = False
    memories: list[MemoryItem] = Field(default_factory=list)


def _recent_user_texts(messages: list, limit: int = 3) -> list[str]:
    """只取用户消息，且最多保留最近 limit 条。"""

    texts: list[str] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            text = message_text(message)
            if text:
                texts.append(text)
    return texts[-limit:]


def _is_sensitive(text: str) -> bool:
    """简单拦截凭证类内容，避免自动记忆泄露密钥。"""

    return bool(_SENSITIVE_PATTERNS.search(text))


def _memory_id(content: str) -> str:
    """同一内容得到同一个 ID，实现幂等 upsert，避免重复记忆。

    归一化时去掉空格和标点，避免“70 公斤”与“70公斤”、“。”结尾
    这类细微差异生成两条记忆。
    """

    normalized = re.sub(r"[\W_]+", "", content.lower())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"memory-{digest}"


class MemoryCurator:
    """可被 LangGraph 调用的自动记忆节点。"""

    def __init__(self, llm: BaseChatModel, knowledge_base: ChromaKnowledgeBase) -> None:
        self._llm = llm
        self._kb = knowledge_base

    def _extract(self, user_texts: list[str]) -> MemoryDecision:
        formatted = "\n".join(
            f"{index}. {text}" for index, text in enumerate(user_texts, start=1)
        )
        response = self._llm.invoke(
            [
                SystemMessage(content=MEMORY_CURATOR_SYSTEM_PROMPT),
                HumanMessage(
                    content=(
                        "下面是用户最近的发言（按时间顺序），"
                        "请判断是否值得写入长期记忆。\n\n"
                        f"{formatted}"
                    )
                ),
            ]
        )
        data = extract_json_object(message_text(response))
        try:
            return MemoryDecision.model_validate(data)
        except ValidationError as exc:
            raise ValueError(f"记忆 JSON 字段不合法：{data!r}") from exc

    def _find_similar_memory(self, content: str) -> str | None:
        """查找已有记忆中的近似条目，命中时复用旧 ID。

        完全相同的内容会被哈希 ID 拦截；这里处理“换一种说法但仍是同一件事”
        的情况，避免长期使用后记忆库被近义句堆满。
        """

        try:
            hits = self._kb.search(content, k=1, where={"source": "auto_memory"})
        except Exception:
            logger.exception("记忆近似去重查询失败")
            return None

        if not hits:
            return None
        hit = hits[0]
        if (
            hit.id
            and hit.distance is not None
            and hit.distance <= _SEMANTIC_DEDUP_DISTANCE
        ):
            return hit.id
        return None

    def __call__(self, state: SecretaryState) -> dict:
        messages = list(state.get("messages", []))
        user_texts = _recent_user_texts(messages)
        if not user_texts:
            return {"memories_saved": 0, "memory_ids": []}

        explicit = any(
            hint in text for text in user_texts for hint in _EXPLICIT_MEMORY_HINTS
        )

        try:
            decision = self._extract(user_texts)
        except Exception as exc:
            logger.exception("自动记忆提取失败")
            return {
                "memories_saved": 0,
                "memory_ids": [],
                "memory_error": f"{type(exc).__name__}: {exc}",
            }

        if not decision.should_store:
            return {"memories_saved": 0, "memory_ids": []}

        now = datetime.now(UTC).isoformat()
        texts: list[str] = []
        metadatas: list[dict] = []
        ids: list[str] = []

        for item in decision.memories[:3]:
            content = " ".join(item.content.split()).strip()
            if not content or _is_sensitive(content):
                continue

            memory_id = _memory_id(content)
            if memory_id in ids:
                continue

            similar_id = self._find_similar_memory(content)
            if similar_id:
                memory_id = similar_id
            if memory_id in ids:
                continue

            texts.append(content)
            ids.append(memory_id)
            metadatas.append(
                {
                    "source": "auto_memory",
                    "topic": item.topic.strip() or "个人",
                    "kind": item.kind,
                    "explicit": explicit,
                    "updated_at": now,
                }
            )

        if not texts:
            return {"memories_saved": 0, "memory_ids": []}

        try:
            self._kb.upsert_texts(texts, metadatas=metadatas, ids=ids)
        except Exception as exc:
            logger.exception("自动记忆写入 Chroma 失败")
            return {
                "memories_saved": 0,
                "memory_ids": [],
                "memory_error": f"{type(exc).__name__}: {exc}",
            }

        return {"memories_saved": len(texts), "memory_ids": ids}
