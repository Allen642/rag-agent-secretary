"""本地 RAG 冒烟测试：ChromaDB + CPU Embedding。

这个脚本与 DeepSeek 无关，用来单独验证向量库持久化和 CPU 检索链路。

运行：
    python scripts/rag_smoke.py

首次运行会下载约 100MB 的 bge-small-zh-v1.5 权重。下载完成后，Embedding
推理完全在本地 CPU 上进行。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许直接执行 `python scripts/rag_smoke.py`，无需先安装成包
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from secretary.rag import ChromaKnowledgeBase


def main() -> int:
    kb = ChromaKnowledgeBase(collection_name="rag_smoke_test")

    documents = [
        "高等数学中，数列极限的严格定义使用 ε-N 语言描述。",
        "C 语言里指针保存变量的内存地址，使用前必须初始化。",
        "PTE 口语部分需要重点练习 Read Aloud 和 Repeat Sentence。",
        "新手健身可以从每周三次全身训练开始，注意动作质量。",
        "Kali Linux 仅应在获得授权的环境中进行安全测试。",
    ]
    metadatas = [
        {"source": "manual", "topic": "高数"},
        {"source": "manual", "topic": "C语言"},
        {"source": "manual", "topic": "PTE"},
        {"source": "manual", "topic": "健身"},
        {"source": "manual", "topic": "Kali"},
    ]

    print("正在写入示例文档（首次运行需要加载 Embedding 模型）……")
    ids = kb.add_texts(documents, metadatas=metadatas)
    print(f"已写入 {len(ids)} 条，当前 collection 共 {kb.count()} 条。")

    query = "极限的 ε-N 定义是什么？"
    print(f"\n查询：{query}")
    results = kb.search(query, k=3)
    for rank, item in enumerate(results, start=1):
        topic = item.metadata.get("topic", "未知")
        distance = f"{item.distance:.4f}" if item.distance is not None else "N/A"
        print(f"{rank}. [{topic}] distance={distance} | {item.text}")

    print("\nRAG 冒烟测试完成。数据目录：", kb.persist_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
