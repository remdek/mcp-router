"""Tests for embedding settings auto-selection."""

from mcp_router.embedding.factory import create_embedding_client
from mcp_router.embedding.local import LocalEmbeddingClient
from mcp_router.embedding.openai_compatible import EmbeddingClient
from mcp_router.models.settings import Settings


def test_use_remote_when_url_and_model_set() -> None:
    settings = Settings(
        embedding_url="http://localhost:1234/v1",
        embedding_model="text-embedding-3-small",
    )
    assert settings.use_remote_embeddings is True
    assert settings.embedding_mode == "remote"
    client = create_embedding_client(settings)
    assert isinstance(client, EmbeddingClient)


def test_use_local_when_url_missing() -> None:
    settings = Settings(embedding_url="", embedding_model="")
    assert settings.use_remote_embeddings is False
    assert settings.embedding_mode == "local"
    client = create_embedding_client(settings)
    assert isinstance(client, LocalEmbeddingClient)
