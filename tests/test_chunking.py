"""Markdown 切块相关测试。"""

from __future__ import annotations

from secretary.rag.chunking import _split_long_text, chunk_markdown


def test_split_long_text_keeps_each_piece_within_limit() -> None:
    text = "\n".join("这是一行用于测试切块长度的内容。" * 4 for _ in range(20))

    pieces = _split_long_text(text, max_chars=100, overlap=20)

    assert len(pieces) > 1
    assert all(len(piece) <= 100 for piece in pieces)


def test_long_single_line_is_hard_split_without_exceeding_limit() -> None:
    text = "长" * 350

    pieces = _split_long_text(text, max_chars=100, overlap=20)

    assert len(pieces) > 1
    assert all(len(piece) <= 100 for piece in pieces)


def test_chunk_ids_are_stable_for_same_structure() -> None:
    content = "# 高数\n\n## 极限\n\n极限的定义。\n\n## 导数\n\n导数的定义。"

    first = chunk_markdown(content, source_path="高数/极限.md", topic="高数")
    second = chunk_markdown(content, source_path="高数/极限.md", topic="高数")

    assert [chunk.id for chunk in first] == [chunk.id for chunk in second]
    assert all(chunk.text.startswith("【高数｜") for chunk in first)
