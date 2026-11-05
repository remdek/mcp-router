"""Create embedding client from settings."""

from __future__ import annotations

import logging

from mcp_router.embedding.local import LocalEmbeddingClient
from mcp_router.embedding.openai_compatible import EmbeddingClient
from mcp_router.embedding.types import EmbeddingProvider
from mcp_router.models.settings import Settings

logger = logging.getLogger(__name__)


def create_embedding_client(settings: Settings) -> EmbeddingProvider | None:
    if not settings.embedding_available:
        return None
    if settings.use_remote_embeddings:
        logger.info(
            "Using remote embeddings: %s @ %s",
            settings.embedding_model,
            settings.embedding_url,
        )
        return EmbeddingClient(
            settings.embedding_url,
            settings.embedding_model,
            settings.embedding_api_key,
        )
    logger.info("Using local embeddings: %s", settings.local_embedding_model)
    return LocalEmbeddingClient(settings.local_embedding_model)
