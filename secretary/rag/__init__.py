"""本地 RAG 组件：CPU Embedding + ChromaDB 持久化 + 文档导入。"""

from .chroma_store import ChromaKnowledgeBase, RetrievedChunk
from .chunking import DocumentChunk, chunk_markdown
from .embeddings import LocalCPUEmbedder
from .ingest import (
    IngestReport,
    ingest_dir,
    ingest_file,
    ingest_pack,
    ingest_text,
)

__all__ = [
    "ChromaKnowledgeBase",
    "DocumentChunk",
    "IngestReport",
    "LocalCPUEmbedder",
    "RetrievedChunk",
    "chunk_markdown",
    "ingest_dir",
    "ingest_file",
    "ingest_pack",
    "ingest_text",
]
