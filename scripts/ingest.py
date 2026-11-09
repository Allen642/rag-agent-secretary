"""文档导入 CLI。

运行：
    python scripts/ingest.py ingest                  # 导入 data/docs 下全部文件
    python scripts/ingest.py ingest --path 某个文件.md
    python scripts/ingest.py ingest --path 某个目录 --topic 高数
    python scripts/ingest.py pack --path pack.zip    # 导入 .zip 知识包
    python scripts/ingest.py add-text "一段资料" --topic 健身
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from secretary.config import get_settings
from secretary.rag import ChromaKnowledgeBase
from secretary.rag.ingest import ingest_dir, ingest_file, ingest_pack, ingest_text


def _configure_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把 Markdown/TXT/zip 导入本地知识库")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="导入文件、目录或 data/docs")
    ingest.add_argument("--path", help="文件或目录；省略时使用 data/docs")
    ingest.add_argument("--topic", help="统一主题标签；省略时按子目录名或文件名推断")

    pack = sub.add_parser("pack", help="导入 .zip 知识包")
    pack.add_argument("--path", required=True)
    pack.add_argument("--topic", help="统一主题标签")

    add_text = sub.add_parser("add-text", help="导入一段文本")
    add_text.add_argument("text")
    add_text.add_argument("--topic", default="通用")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    args = build_parser().parse_args(argv)
    settings = get_settings()
    kb = ChromaKnowledgeBase()

    print(f"知识库目录：{settings.chroma_dir}")
    print(f"文档目录：{settings.docs_dir}")

    if args.command == "ingest":
        target = Path(args.path).expanduser() if args.path else settings.docs_dir
        if not target.is_absolute():
            target = (PROJECT_ROOT / target).resolve()

        if target.is_file():
            result = ingest_file(kb, target, topic=args.topic)
            print(f"导入 1 个文件：{result.chunks} 段，跳过 {result.skipped} 段")
        elif target.is_dir():
            print(f"导入目录：{target}")
            result = ingest_dir(kb, target, topic=args.topic)
            print(
                f"完成：{result.files} 个文件，{result.chunks} 段，跳过 {result.skipped} 段"
            )
        else:
            print(f"路径不存在：{target}")
            return 2

    elif args.command == "pack":
        target = Path(args.path).expanduser()
        if not target.is_absolute():
            target = (PROJECT_ROOT / target).resolve()
        print(f"导入知识包：{target}")
        result = ingest_pack(kb, target, topic=args.topic)
        print(
            f"完成：{result.files} 个文件，{result.chunks} 段，跳过 {result.skipped} 段"
        )

    elif args.command == "add-text":
        doc_id = ingest_text(kb, args.text, topic=args.topic, source="doc")
        print(f"已写入：{doc_id}")

    print(f"当前知识库总条数：{kb.count()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
