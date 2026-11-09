"""Markdown 文档切块。

设计目标：
  * 按标题层级切分，保留“文件 > 一级标题 > 二级标题”的上下文路径；
  * 超长小节再按行滑窗切分，避免单条片段超过 Embedding 模型的最佳长度；
  * 每个片段带稳定 ID（文件路径 + 标题路径 + 序号），重复导入时覆盖而不是重复堆积。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# 约 500 字一条；BGE 中文模型对 500 字以内片段效果较稳
DEFAULT_MAX_CHARS = 500
DEFAULT_OVERLAP = 64


@dataclass(slots=True)
class DocumentChunk:
    """一个可写入向量库的片段。"""

    text: str
    metadata: dict = field(default_factory=dict)
    id: str = ""


def _split_long_text(text: str, max_chars: int, overlap: int) -> list[str]:
    """按行滑窗切分超长文本，尽量在行边界断开。"""

    if len(text) <= max_chars:
        return [text]

    lines = [line for line in text.splitlines() if line.strip()]
    pieces: list[str] = []
    buffer = ""
    for line in lines:
        if len(line) > max_chars:
            # 极长的单行：直接硬切
            if buffer:
                pieces.append(buffer)
                buffer = ""
            for start in range(0, len(line), max_chars - overlap):
                pieces.append(line[start : start + max_chars])
            continue

        candidate = f"{buffer}\n{line}" if buffer else line
        if len(candidate) <= max_chars:
            buffer = candidate
        else:
            pieces.append(buffer)
            buffer = line

    if buffer:
        pieces.append(buffer)

    # 简单重叠：把上一段末尾拼到下一段开头，减少跨段语义断裂
    if overlap > 0 and len(pieces) > 1:
        merged = [pieces[0]]
        for index in range(1, len(pieces)):
            tail = merged[-1][-overlap:]
            merged.append(f"{tail}\n{pieces[index]}")
        return merged
    return pieces


def chunk_markdown(
    content: str,
    *,
    source_path: str,
    topic: str | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> list[DocumentChunk]:
    """把一篇 Markdown 切成带上下文前缀的片段。"""

    lines = content.replace("\r\n", "\n").split("\n")
    # 记录每级标题的当前值，用于生成“标题路径”
    heading_stack: list[tuple[int, str]] = []
    sections: list[tuple[str, str]] = []  # (标题路径, 正文)
    current_path = ""
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            sections.append((current_path, body))

    for line in lines:
        match = _HEADING_RE.match(line)
        if match:
            flush()
            buffer = []
            level = len(match.group(1))
            title = match.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            current_path = " > ".join(item[1] for item in heading_stack)
            continue
        buffer.append(line)
    flush()

    if not sections:
        return []

    chunks: list[DocumentChunk] = []
    for section_index, (path, body) in enumerate(sections):
        if topic:
            prefix = f"【{topic}｜{path}】" if path else f"【{topic}】"
        else:
            prefix = f"【{path}】" if path else ""
        for piece_index, piece in enumerate(_split_long_text(body, max_chars, overlap)):
            text = f"{prefix}\n{piece}".strip()
            digest = hashlib.sha256(
                f"{source_path}|{section_index}|{piece_index}".encode()
            ).hexdigest()[:20]
            chunks.append(
                DocumentChunk(
                    text=text,
                    metadata={"path": path, "source_path": source_path},
                    id=f"doc-{digest}",
                )
            )
    return chunks


def is_supported_file(path: Path) -> bool:
    return path.suffix.lower() in {".md", ".markdown", ".txt"}
