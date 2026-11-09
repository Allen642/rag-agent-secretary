"""预下载本地 Embedding 模型。

运行：
    python scripts/download_model.py

默认使用 https://hf-mirror.com；也可以通过 HF_ENDPOINT 覆盖。
下载完成后会把模型缓存到 Hugging Face 默认缓存目录，并打印可写入
.env 的 EMBEDDING_MODEL 路径。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from secretary.config import get_settings


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    settings = get_settings()
    model = settings.embedding_model
    model_path = Path(model).expanduser()
    if model_path.exists():
        print(f"检测到本地模型目录，无需下载：{model_path.resolve()}")
        return 0

    print(f"HF_ENDPOINT = {os.getenv('HF_ENDPOINT')}")
    print(f"正在下载模型：{model}")
    print("首次下载约 100MB，请耐心等待。")

    from huggingface_hub import snapshot_download

    path = snapshot_download(repo_id=model)
    print("\n下载完成。")
    print(f"模型缓存路径：{path}")
    print("\n可以把下面这行写入 .env，强制使用该本地目录：")
    print(f"EMBEDDING_MODEL={path}")
    print("\n之后可设置以下变量彻底离线运行：")
    print("HF_HUB_OFFLINE=1")
    print("TRANSFORMERS_OFFLINE=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
