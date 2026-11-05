"""OpenAI-compatible embedding client."""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class EmbeddingClient:
    def __init__(self, base_url: str, model: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self._client: httpx.AsyncClient | None = None
        self._dimension: int | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=60.0)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            response = await client.get("/models")
            return response.status_code < 500
        except Exception as exc:
            logger.debug("Embedding health check failed: %s", exc)
            return False

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        client = await self._get_client()
        response = await client.post("/embeddings", json={"model": self.model, "input": texts})
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        items = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
        vectors = [item["embedding"] for item in items]
        if vectors and self._dimension is None:
            self._dimension = len(vectors[0])
        return vectors

    @property
    def dimension(self) -> int | None:
        return self._dimension
