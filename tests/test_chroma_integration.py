"""使用真实 ChromaDB 验证导入更新逻辑（Embedding 用假向量，避免下载模型）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from secretary.rag.chroma_store import ChromaKnowledgeBase
from secretary.rag.ingest import ingest_file


class FakeEmbedder:
    """只返回固定维度的假向量，测试不需要加载 sentence-transformers。"""

    def embed_documents(self, texts):
        return [[float(len(text)), 1.0, 0.0] for text in texts]

    def embed_query(self, query):
        return [float(len(query)), 1.0, 0.0]


DOC_V1 = """# 高数

## 极限

版本一。

## 导数

导数内容。
"""

DOC_V2 = """# 高数

## 极限

版本二。
"""


def test_reimport_updates_and_prunes_real_chroma(tmp_path: Path) -> None:
    root = tmp_path / "docs"
    path = root / "高数" / "极限.md"
    path.parent.mkdir(parents=True)
    path.write_text(DOC_V1, encoding="utf-8")

    kb = ChromaKnowledgeBase(
        persist_dir=tmp_path / "chroma",
        collection_name="ingest_test",
        embedder=FakeEmbedder(),
    )

    first = ingest_file(kb, path, root=root)
    assert (first.chunks, first.skipped, first.deleted) == (2, 0, 0)
    assert kb.count() == 2

    unchanged = ingest_file(kb, path, root=root)
    assert (unchanged.chunks, unchanged.skipped, unchanged.deleted) == (0, 2, 0)
    assert kb.count() == 2

    path.write_text(DOC_V2, encoding="utf-8")
    updated = ingest_file(kb, path, root=root)
    assert (updated.chunks, updated.skipped, updated.deleted) == (1, 0, 1)
    assert kb.count() == 1

    remaining = kb.list_texts(limit=None)
    assert len(remaining) == 1
    assert "版本二" in remaining[0].text
