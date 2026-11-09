"""本地知识库管理 CLI。

用途：
  * 查看自动记忆是否正确；
  * 手动补充资料；
  * 删除错误记忆或过期资料。

运行：
    python scripts/kb.py stats
    python scripts/kb.py list --source auto_memory
    python scripts/kb.py search "我的健身习惯"
    python scripts/kb.py add "极限的 ε-N 定义是……" --topic 高数
    python scripts/kb.py delete memory-xxxxxxxx
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from secretary.rag import ChromaKnowledgeBase


def _configure_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _print_chunk(index: int, chunk, *, show_distance: bool = False) -> None:
    metadata = chunk.metadata
    source = metadata.get("source", "unknown")
    topic = metadata.get("topic", "-")
    distance = (
        f" | distance={chunk.distance:.4f}"
        if show_distance and chunk.distance is not None
        else ""
    )
    print(f"{index}. id={chunk.id} | source={source} | topic={topic}{distance}")
    print(f"   {chunk.text}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="个人知识库管理")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="手动新增或更新一条资料")
    add.add_argument("text", help="资料正文")
    add.add_argument("--topic", default="通用", help="主题标签，例如 高数/C语言/健身")
    add.add_argument("--id", help="指定 ID 时执行 upsert（更新已有记录）")

    search = sub.add_parser("search", help="检索知识库")
    search.add_argument("query")
    search.add_argument("-k", type=int, default=5, help="返回条数，默认 5")

    show = sub.add_parser("list", help="按写入顺序列出内容")
    show.add_argument("--limit", type=int, default=20)
    show.add_argument("--offset", type=int, default=0)
    show.add_argument(
        "--source",
        choices=["auto_memory", "manual"],
        help="只看自动记忆或手动资料",
    )

    delete = sub.add_parser("delete", help="按 ID 删除资料/记忆")
    delete.add_argument("ids", nargs="+")

    sub.add_parser("stats", help="查看知识库统计")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    args = build_parser().parse_args(argv)
    kb = ChromaKnowledgeBase()

    if args.command == "add":
        now = datetime.now(UTC).isoformat()
        metadata = {"source": "manual", "topic": args.topic, "updated_at": now}
        if args.id:
            ids = kb.upsert_texts([args.text], metadatas=[metadata], ids=[args.id])
            print(f"已更新：{ids[0]}")
        else:
            ids = kb.add_texts([args.text], metadatas=[metadata])
            print(f"已写入：{ids[0]}")
        return 0

    if args.command == "search":
        chunks = kb.search(args.query, k=args.k)
        if not chunks:
            print("没有检索到内容。")
            return 0
        for index, chunk in enumerate(chunks, start=1):
            _print_chunk(index, chunk, show_distance=True)
        return 0

    if args.command == "list":
        where = {"source": args.source} if args.source else None
        chunks = kb.list_texts(limit=args.limit, offset=args.offset, where=where)
        if not chunks:
            print("知识库为空，或没有符合条件的内容。")
            return 0
        for index, chunk in enumerate(chunks, start=1):
            _print_chunk(index, chunk)
        return 0

    if args.command == "delete":
        kb.delete_texts(ids=args.ids)
        print(f"已删除 {len(args.ids)} 条：{', '.join(args.ids)}")
        return 0

    if args.command == "stats":
        total = kb.count()
        memories = kb.count(where={"source": "auto_memory"})
        manual = kb.count(where={"source": "manual"})
        print(f"总条数：{total}")
        print(f"自动记忆：{memories} 条")
        print(f"手动资料：{manual} 条")
        print(f"数据目录：{kb.persist_dir}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
