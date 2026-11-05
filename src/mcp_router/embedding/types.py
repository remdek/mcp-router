"""Embedding provider protocol."""

from typing import Protocol


class EmbeddingProvider(Protocol):
    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...

    async def health_check(self) -> bool: ...

    async def close(self) -> None: ...

    @property
    def dimension(self) -> int | None: ...
