"""文档导入/重复导入一致性测试。"""

from __future__ import annotations

from pathlib import Path

from secretary.rag.chroma_store import RetrievedChunk
from secretary.rag.ingest import ingest_file


class FakeKnowledgeBase:
    """只实现 ingest_file 用到的方法，避免测试依赖 Chroma 与 Embedding。"""

    def __init__(self) -> None:
        self.items: dict[str, RetrievedChunk] = {}

    def list_texts(
        self,
        *,
        limit: int | None = 20,
        offset: int = 0,
        where: dict | None = None,
    ) -> list[RetrievedChunk]:
        values = list(self.items.values())
        if where:
            values = [
                item
                for item in values
                if all(item.metadata.get(key) == value for key, value in where.items())
            ]
        values = values[offset:]
        if limit is not None:
            values = values[:limit]
        return values

    def upsert_texts(self, texts, *, metadatas=None, ids=None) -> list[str]:
        metadatas = metadatas or [{} for _ in texts]
        ids = ids or [f"doc-{index}" for index in range(len(texts))]
        for text, metadata, doc_id in zip(texts, metadatas, ids, strict=True):
            self.items[doc_id] = RetrievedChunk(
                text=text,
                metadata=dict(metadata),
                id=doc_id,
            )
        return list(ids)

    def delete_texts(self, *, ids=None, where=None) -> None:
        if ids:
            for doc_id in ids:
                self.items.pop(doc_id, None)
            return
        if where:
            for doc_id, item in list(self.items.items()):
                if all(
                    item.metadata.get(key) == value
                    for key, value in where.items()
                ):
                    self.items.pop(doc_id, None)

    def count(self, *, where: dict | None = None) -> int:
        return len(self.list_texts(limit=None, where=where))


DOC = """# 高数

## 极限

版本一。

## 导数

导数内容。
"""


def _write_doc(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "高数" / "极限.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_first_import_writes_all_chunks(tmp_path: Path) -> None:
    kb = FakeKnowledgeBase()

    report = ingest_file(kb, _write_doc(tmp_path, DOC), root=tmp_path)

    assert (report.files, report.chunks, report.skipped, report.deleted) == (
        1,
        2,
        0,
        0,
    )
    assert len(kb.items) == 2


def test_reimport_unchanged_file_skips_all_chunks(tmp_path: Path) -> None:
    kb = FakeKnowledgeBase()
    path = _write_doc(tmp_path, DOC)
    ingest_file(kb, path, root=tmp_path)

    report = ingest_file(kb, path, root=tmp_path)

    assert (report.files, report.chunks, report.skipped, report.deleted) == (
        1,
        0,
        2,
        0,
    )
    assert len(kb.items) == 2


def test_reimport_updates_changed_chunk(tmp_path: Path) -> None:
    kb = FakeKnowledgeBase()
    path = _write_doc(tmp_path, DOC)
    ingest_file(kb, path, root=tmp_path)

    path.write_text(DOC.replace("版本一。", "版本二。"), encoding="utf-8")
    report = ingest_file(kb, path, root=tmp_path)

    assert (report.files, report.chunks, report.skipped, report.deleted) == (
        1,
        1,
        1,
        0,
    )
    texts = [item.text for item in kb.items.values()]
    assert any("版本二" in text for text in texts)
    assert all("版本一" not in text for text in texts)


def test_reimport_removes_deleted_section(tmp_path: Path) -> None:
    kb = FakeKnowledgeBase()
    path = _write_doc(tmp_path, DOC)
    ingest_file(kb, path, root=tmp_path)

    path.write_text("# 高数\n\n## 极限\n\n版本一。\n", encoding="utf-8")
    report = ingest_file(kb, path, root=tmp_path)

    assert (report.files, report.chunks, report.skipped, report.deleted) == (
        1,
        0,
        1,
        1,
    )
    texts = [item.text for item in kb.items.values()]
    assert all("导数内容" not in text for text in texts)


def test_reimport_with_new_topic_updates_metadata(tmp_path: Path) -> None:
    kb = FakeKnowledgeBase()
    path = _write_doc(tmp_path, DOC)
    ingest_file(kb, path, root=tmp_path)

    report = ingest_file(kb, path, root=tmp_path, topic="数学")

    assert report.chunks == 2
    assert report.skipped == 0
    assert {item.metadata["topic"] for item in kb.items.values()} == {"数学"}
