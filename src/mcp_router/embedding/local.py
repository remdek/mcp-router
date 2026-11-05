"""Local CPU embeddings via fastembed (ONNX, no external API)."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


class LocalEmbeddingClient:
    """ONNX-based embeddings running on CPU inside the container."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._model: object | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embed")
        self._dimension: int | None = None

    def _get_model(self) -> object:
        if self._model is None:
            from fastembed import TextEmbedding  # type: ignore[import-untyped]

            logger.info("Loading local embedding model: %s", self.model_name)
            self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    def _encode_sync(self, texts: list[str]) -> list[list[float]]:
        model = self._get_model()
        vectors = [vec.tolist() for vec in model.embed(texts)]  # type: ignore[attr-defined]
        if vectors and self._dimension is None:
            self._dimension = len(vectors[0])
        return vectors

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._encode_sync, texts)

    async def health_check(self) -> bool:
        try:
            vectors = await self.embed_texts(["health"])
            return bool(vectors)
        except Exception as exc:
            logger.debug("Local embedding health check failed: %s", exc)
            return False

    async def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._model = None

    @property
    def dimension(self) -> int | None:
        return self._dimension
