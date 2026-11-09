"""本地 CPU 向量化。

这里不依赖任何在线 Embedding API。模型通过 sentence-transformers 在
首次使用时才加载，避免只跑对话骨架时也占用内存。
"""

from __future__ import annotations

from collections.abc import Sequence

from ..config import get_settings


class LocalCPUEmbedder:
    """把文本转换成向量，强制使用 CPU。"""

    def __init__(
        self,
        model_name: str | None = None,
        *,
        device: str = "cpu",
        batch_size: int = 32,
    ) -> None:
        if device.lower() != "cpu":
            # 明确拒绝 CUDA，防止在无显卡机器上意外触发 GPU 初始化
            raise ValueError("本项目只支持 CPU 推理，device 必须为 'cpu'")

        settings = get_settings()
        self.model_name = model_name or settings.embedding_model
        self.device = "cpu"
        self.batch_size = batch_size
        self._model = None  # 延迟加载，第一次调用 embed_* 时才下载/载入模型

    def _load_model(self):
        if self._model is None:
            # 延迟 import：不跑 RAG 时无需加载 torch，启动更快
            from sentence_transformers import SentenceTransformer

            try:
                self._model = SentenceTransformer(self.model_name, device=self.device)
            except Exception as exc:
                raise RuntimeError(
                    "本地 Embedding 模型加载失败。\n"
                    f"模型：{self.model_name}\n"
                    "请检查网络或镜像设置。国内网络建议使用：\n"
                    '  $env:HF_ENDPOINT="https://hf-mirror.com"\n'
                    "或先按 README 的“预下载模型”步骤把模型放入缓存。"
                ) from exc
        return self._model

    def _encode(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []

        model = self._load_model()
        vectors = model.encode(
            list(texts),
            batch_size=self.batch_size,
            show_progress_bar=False,
            normalize_embeddings=True,  # 归一化后可用余弦距离比较
            convert_to_numpy=True,
        )
        return vectors.tolist()

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """文档向量。"""

        return self._encode(texts)

    def embed_query(self, query: str) -> list[float]:
        """查询向量；bge-small-zh-v1.5 不需要额外的 query 指令前缀。"""

        vectors = self._encode([query])
        return vectors[0] if vectors else []
