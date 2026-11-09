"""集中管理环境变量、路径和运行参数。

所有模块都通过 ``get_settings()`` 读取配置，避免在业务代码里到处写
``os.getenv``。以后新增配置项时，只需要改这一个文件。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录：secretary/config.py 的上一级
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
DATA_DIR = PROJECT_ROOT / "data"
# 运行时数据（个人记忆、向量库）放这里，不提交到 GitHub
DEFAULT_CHROMA_DIR = DATA_DIR / "runtime" / "chroma"
# 个人文档：可以放自己的笔记/档案，也会被 .gitignore 忽略
DEFAULT_DOCS_DIR = DATA_DIR / "docs"
# 示例文档：公开可提交，clone 后不用任何隐私数据也能体验导入流程
EXAMPLE_DOCS_DIR = DATA_DIR / "example"

# override=False：系统环境变量优先于 .env，方便 CI/服务器覆盖本地配置
load_dotenv(ENV_FILE, override=False)

# 国内网络访问 huggingface.co 经常超时。默认走镜像；如需官方源，
# 在 .env 或系统环境变量中显式设置 HF_ENDPOINT=https://huggingface.co 即可。
DEFAULT_HF_ENDPOINT = "https://hf-mirror.com"
if not os.getenv("HF_ENDPOINT", "").strip():
    os.environ["HF_ENDPOINT"] = DEFAULT_HF_ENDPOINT


@dataclass(frozen=True, slots=True)
class Settings:
    """一次进程运行期间使用的不可变配置。"""

    deepseek_api_key: str | None
    deepseek_base_url: str
    deepseek_model: str
    chroma_dir: Path
    docs_dir: Path
    embedding_model: str
    embedding_device: str = "cpu"

    @property
    def has_api_key(self) -> bool:
        """只判断 key 是否存在，不暴露 key 的内容。"""

        return bool(self.deepseek_api_key)


def _resolve_path(raw_value: str) -> Path:
    """把相对路径统一解析到项目根目录，避免受启动目录影响。"""

    path = Path(raw_value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """读取并缓存配置；多次调用不会重复解析 .env。"""

    return Settings(
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", "").strip() or None,
        deepseek_base_url=os.getenv(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
        ).strip(),
        deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip(),
        chroma_dir=_resolve_path(os.getenv("CHROMA_DIR", str(DEFAULT_CHROMA_DIR))),
        docs_dir=_resolve_path(os.getenv("DOCS_DIR", str(DEFAULT_DOCS_DIR))),
        embedding_model=os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5").strip(),
        # 本项目明确只做 CPU 推理，默认值不提供 CUDA 选项
        embedding_device=os.getenv("EMBEDDING_DEVICE", "cpu").strip() or "cpu",
    )


def require_api_key(settings: Settings | None = None) -> str:
    """在真正创建在线模型前检查 Key，并给出可操作的报错。"""

    settings = settings or get_settings()
    placeholder = settings.deepseek_api_key == "sk-your-deepseek-api-key"
    if not settings.deepseek_api_key or placeholder:
        raise RuntimeError(
            "未找到有效的 DEEPSEEK_API_KEY。\n"
            "请复制 .env.example 为 .env，并填入你的 DeepSeek API Key；"
            "或先在当前终端执行：\n"
            '  $env:DEEPSEEK_API_KEY="sk-..."   # PowerShell\n'
            "如果只想验证路由和工具调用链路，可运行：\n"
            "  python test_run.py --offline"
        )
    return settings.deepseek_api_key
