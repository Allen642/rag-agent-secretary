"""基于 ChromaDB PersistentClient 的本地知识库（可读、可写、可删）。

每类知识（高数、C 语言、PTE、健身……）可以各自使用一个 collection，
也可以共享一个 collection 并通过 metadata 过滤。自动记忆和普通资料
默认共用 ``personal_knowledge``，用 metadata["source"] 区分来源。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..config import get_settings
from .embeddings import LocalCPUEmbedder


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """检索/列表结果的最小结构，方便直接转成 Tool 的返回文本。"""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    distance: float | None = None
    id: str | None = None


class ChromaKnowledgeBase:
    """ChromaDB 本地持久化封装。

    使用方式：
        kb = ChromaKnowledgeBase()
        kb.add_texts(["极限的定义是……"], metadatas=[{"topic": "高数"}])
        kb.upsert_texts(["更新后的内容"], ids=["doc-1"])
        chunks = kb.search("极限", k=3)
    """

    def __init__(
        self,
        *,
        persist_dir: Path | str | None = None,
        collection_name: str = "personal_knowledge",
        embedder: LocalCPUEmbedder | None = None,
    ) -> None:
        settings = get_settings()
        self.persist_dir = (
            Path(persist_dir).expanduser().resolve()
            if persist_dir is not None
            else settings.chroma_dir
        )
        self.collection_name = collection_name
        self._embedder = embedder or LocalCPUEmbedder(
            settings.embedding_model, device=settings.embedding_device
        )
        self._client = None
        self._collection = None

    def _get_collection(self):
        """按需创建 PersistentClient，避免导入模块时就锁住数据目录。"""

        if self._collection is None:
            import chromadb

            self.persist_dir.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self.persist_dir))
            self._collection = self._client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection

    def _prepare(
        self,
        texts: Sequence[str],
        metadatas: Sequence[dict[str, Any]] | None,
        ids: Sequence[str] | None,
    ) -> tuple[list[str], dict[str, Any]]:
        """校验输入并生成 Chroma 写入参数；add 和 upsert 共用。"""

        cleaned = [text.strip() for text in texts]
        if any(not text for text in cleaned):
            raise ValueError("texts 中不能包含空字符串")
        if metadatas is not None and len(metadatas) != len(cleaned):
            raise ValueError("metadatas 的长度必须与 texts 一致")
        if ids is not None and len(ids) != len(cleaned):
            raise ValueError("ids 的长度必须与 texts 一致")

        doc_ids = (
            list(ids) if ids is not None else [f"doc-{uuid4().hex}" for _ in cleaned]
        )
        embeddings = self._embedder.embed_documents(cleaned)

        payload: dict[str, Any] = {
            "ids": doc_ids,
            "documents": cleaned,
            "embeddings": embeddings,
        }
        if metadatas is not None:
            payload["metadatas"] = [dict(item) for item in metadatas]
        return doc_ids, payload

    def add_texts(
        self,
        texts: Sequence[str],
        *,
        metadatas: Sequence[dict[str, Any]] | None = None,
        ids: Sequence[str] | None = None,
    ) -> list[str]:
        """新增文本。ID 已存在时 Chroma 会报错；需要覆盖请用 upsert_texts。"""

        doc_ids, payload = self._prepare(texts, metadatas, ids)
        self._get_collection().add(**payload)
        return doc_ids

    def upsert_texts(
        self,
        texts: Sequence[str],
        *,
        metadatas: Sequence[dict[str, Any]] | None = None,
        ids: Sequence[str] | None = None,
    ) -> list[str]:
        """按 ID 新增或覆盖文本，适合持续更新记忆和资料。"""

        doc_ids, payload = self._prepare(texts, metadatas, ids)
        self._get_collection().upsert(**payload)
        return doc_ids

    def search(
        self,
        query: str,
        *,
        k: int = 4,
        where: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        """按余弦距离召回最相近的 k 个片段。"""

        query = query.strip()
        if not query:
            return []
        if k <= 0:
            raise ValueError("k 必须大于 0")

        collection = self._get_collection()
        total = collection.count()
        if total == 0:
            return []

        query_kwargs: dict[str, Any] = {
            "query_embeddings": [self._embedder.embed_query(query)],
            "n_results": min(k, total),
            "include": ["documents", "metadatas", "distances"],
        }
        if where is not None:
            query_kwargs["where"] = where

        result = collection.query(**query_kwargs)

        # Chroma 的返回结构是“二维列表”：外层对应每个查询，这里只有一个查询
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]

        chunks: list[RetrievedChunk] = []
        for index, document in enumerate(documents):
            metadata = metadatas[index] if index < len(metadatas) else {}
            distance = distances[index] if index < len(distances) else None
            chunks.append(
                RetrievedChunk(
                    text=document or "",
                    metadata=dict(metadata or {}),
                    distance=float(distance) if distance is not None else None,
                    id=ids[index] if index < len(ids) else None,
                )
            )
        return chunks

    def list_texts(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        where: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        """按写入顺序列出文档，用于管理 CLI 查看/纠错。"""

        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        if offset < 0:
            raise ValueError("offset 不能为负数")

        get_kwargs: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "include": ["documents", "metadatas"],
        }
        if where is not None:
            get_kwargs["where"] = where

        result = self._get_collection().get(**get_kwargs)
        ids = result.get("ids") or []
        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []

        return [
            RetrievedChunk(
                text=document or "",
                metadata=dict(metadatas[index] or {}) if index < len(metadatas) else {},
                id=ids[index] if index < len(ids) else None,
            )
            for index, document in enumerate(documents)
        ]

    def delete_texts(
        self,
        *,
        ids: Sequence[str] | None = None,
        where: dict[str, Any] | None = None,
    ) -> None:
        """按 ID 或 metadata 条件删除文档；两者至少提供一个。"""

        if not ids and where is None:
            raise ValueError("删除时必须提供 ids 或 where")

        kwargs: dict[str, Any] = {}
        if ids:
            kwargs["ids"] = list(ids)
        if where is not None:
            kwargs["where"] = where
        self._get_collection().delete(**kwargs)

    def get_metadatas(self, ids: Sequence[str]) -> list[dict[str, Any]]:
        """按 ID 批量读取 metadata，用于导入时判断内容是否需要更新。"""

        if not ids:
            return []
        result = self._get_collection().get(ids=list(ids), include=["metadatas"])
        return [dict(metadata or {}) for metadata in (result.get("metadatas") or [])]

    def count(self, *, where: dict[str, Any] | None = None) -> int:
        """当前 collection 中的文档数；可带 metadata 过滤。"""

        collection = self._get_collection()
        if where is None:
            return collection.count()
        result = collection.get(where=where, include=[])
        return len(result.get("ids") or [])
