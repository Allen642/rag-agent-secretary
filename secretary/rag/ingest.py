"""把文本/文件导入本地知识库。

三种入口：
  * ``ingest_text``：导入一段临时文本（CLI 的 add-text）；
  * ``ingest_path``：导入 Markdown/TXT 文件或整个目录；
  * ``ingest_pack``：导入 .zip 知识包（先解压到临时目录再导入）。

所有片段都使用稳定 ID，同一个文件重复导入会覆盖旧内容，不会重复堆积。
"""

from __future__ import annotations

import logging
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ..config import get_settings
from .chroma_store import ChromaKnowledgeBase
from .chunking import DocumentChunk, chunk_markdown, is_supported_file

logger = logging.getLogger(__name__)

# 向量库用余弦距离；超过这个阈值的片段视为不相关
_DEDUP_DISTANCE = 0.10


@dataclass(slots=True)
class IngestReport:
    files: int = 0
    chunks: int = 0
    skipped: int = 0


def _read_text(path: Path) -> str:
    # utf-8-sig 兼容 Windows 记事本/Excel 导出的 BOM
    for encoding in ("utf-8-sig", "gb18030", "utf-8"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("utf-8", b"", 0, 1, f"无法识别编码：{path}")


def _topic_for(path: Path, root: Path, fallback: str) -> str:
    """优先用子目录名作为主题；根目录文件用文件名（去掉 # 和括号后缀）。"""

    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = Path(path.name)
    if len(relative.parts) > 1:
        return relative.parts[0]
    if fallback:
        return fallback

    name = path.stem.lstrip("#＃ ").strip()
    # “个人健身档案与训练指南 (RAG 知识库)” -> “个人健身档案与训练指南”
    name = re.sub(r"\s*[（(][^）)]*[）)]\s*$", "", name).strip()
    return name or path.stem


def ingest_text(
    kb: ChromaKnowledgeBase,
    text: str,
    *,
    topic: str = "通用",
    source: str = "manual",
    doc_id: str | None = None,
) -> str:
    """导入一段文本。source=doc 时参与文档去重。"""

    cleaned = text.strip()
    if not cleaned:
        raise ValueError("文本不能为空")

    now = datetime.now(UTC).isoformat()
    metadata = {"source": source, "topic": topic, "updated_at": now}

    if source == "doc":
        hits = kb.search(cleaned, k=1, where={"source": "doc"})
        if (
            hits
            and hits[0].distance is not None
            and hits[0].distance <= _DEDUP_DISTANCE
        ):
            # 已有近似内容：复用命中 ID 覆盖，避免重复
            hit = hits[0]
            if hit.id:
                metadata["path"] = ""
                kb.upsert_texts([cleaned], metadatas=[metadata], ids=[hit.id])
                return hit.id

    new_id = doc_id or f"doc-{uuid4().hex[:20]}"
    kb.upsert_texts([cleaned], metadatas=[metadata], ids=[new_id])
    return new_id


def _existing_metadata(kb: ChromaKnowledgeBase, doc_id: str) -> dict | None:
    """按 ID 查询已有片段的 metadata；没有则返回 None。"""

    metas = kb.get_metadatas([doc_id])
    return metas[0] if metas and metas[0] else None


def _same_chunk_exists(
    kb: ChromaKnowledgeBase, chunk: DocumentChunk, *, topic: str
) -> bool:
    """判断该片段是否已经以相同主题和来源路径导入过。

    稳定 ID 由“文件 + 小节 + 分片序号”决定，所以同一内容会命中同一个 ID；
    只有主题或来源路径也一致时才跳过，方便改文件名/改主题后覆盖旧记录。
    """

    existing = _existing_metadata(kb, chunk.id)
    if existing is None:
        return False
    existing_path = existing.get("source_path")
    if existing_path is None:
        # 旧版本写入的记录没有 source_path，按主题一致即可跳过
        return existing.get("topic") == topic
    return existing.get("topic") == topic and existing_path == chunk.metadata.get(
        "source_path"
    )


def ingest_file(
    kb: ChromaKnowledgeBase,
    path: Path,
    *,
    root: Path | None = None,
    topic: str | None = None,
) -> IngestReport:
    """导入单个 Markdown/TXT 文件，按标题切块。"""

    report = IngestReport()
    content = _read_text(path)
    base_root = root or path.parent
    resolved_topic = topic or _topic_for(path, base_root, "")

    chunks = chunk_markdown(
        content,
        source_path=str(path.relative_to(base_root))
        if path.is_relative_to(base_root)
        else path.name,
        topic=resolved_topic,
    )
    if not chunks:
        report.skipped += 1
        return report

    report.files += 1
    texts: list[str] = []
    metadatas: list[dict] = []
    ids: list[str] = []
    now = datetime.now(UTC).isoformat()

    for chunk in chunks:
        # 主题/源文件变了也算更新：允许改文件名、改主题后覆盖旧片段
        if _same_chunk_exists(kb, chunk, topic=resolved_topic):
            report.skipped += 1
            continue
        texts.append(chunk.text)
        ids.append(chunk.id)
        metadatas.append(
            {
                "source": "doc",
                "topic": resolved_topic,
                "path": chunk.metadata.get("path", ""),
                "source_path": chunk.metadata.get("source_path", path.name),
                "updated_at": now,
            }
        )

    if texts:
        kb.upsert_texts(texts, metadatas=metadatas, ids=ids)
        report.chunks = len(texts)
    return report


def iter_supported_files(root: Path, *, exclude_dirs: Iterable[str] = ()) -> list[Path]:
    excluded = set(exclude_dirs)
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in excluded for part in path.relative_to(root).parts):
            continue
        if is_supported_file(path):
            files.append(path)
    return files


def ingest_dir(
    kb: ChromaKnowledgeBase,
    directory: Path,
    *,
    topic: str | None = None,
) -> IngestReport:
    """导入目录下所有 Markdown/TXT 文件。"""

    if not directory.exists():
        raise FileNotFoundError(f"目录不存在：{directory}")
    if not directory.is_dir():
        raise NotADirectoryError(f"不是目录：{directory}")

    total = IngestReport()
    for path in iter_supported_files(directory):
        result = ingest_file(kb, path, root=directory, topic=topic)
        total.files += result.files
        total.chunks += result.chunks
        total.skipped += result.skipped
        print(f"  {path.relative_to(directory)} -> {result.chunks} 段")
    return total


def ingest_pack(
    kb: ChromaKnowledgeBase,
    pack: Path,
    *,
    topic: str | None = None,
) -> IngestReport:
    """导入 .zip 知识包。

    压缩包内所有 Markdown/TXT 都会被导入；如果包里有 manifest.json，
    其 topic 字段会作为默认主题。
    """

    import json
    import tempfile

    if not pack.is_file():
        raise FileNotFoundError(f"知识包不存在：{pack}")

    with tempfile.TemporaryDirectory(prefix="kb-pack-") as temp_dir:
        with zipfile.ZipFile(pack) as archive:
            # 防目录穿越：只解压到临时目录，并拒绝绝对路径
            for member in archive.infolist():
                if (
                    member.filename.startswith(("/", "\\"))
                    or ".." in Path(member.filename).parts
                ):
                    raise ValueError(f"知识包含有非法路径：{member.filename}")
            archive.extractall(temp_dir)

        root = Path(temp_dir)
        pack_topic = topic
        manifest = root / "manifest.json"
        if pack_topic is None and manifest.exists():
            try:
                pack_topic = json.loads(manifest.read_text(encoding="utf-8")).get(
                    "topic"
                )
            except (json.JSONDecodeError, OSError):
                logger.warning("manifest.json 解析失败，忽略主题字段")

        return ingest_dir(kb, root, topic=pack_topic)


def default_docs_dir() -> Path:
    return get_settings().docs_dir
