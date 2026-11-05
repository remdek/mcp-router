"""Index tools into SQLite storage."""

import logging
from typing import Any

from mcp_router.embedding.types import EmbeddingProvider
from mcp_router.index.extractor import ToolExtractor
from mcp_router.models.tools import IndexStatus
from mcp_router.services.aliases import AliasCatalog
from mcp_router.services.server_manager import ServerManager
from mcp_router.storage.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)


class ToolIndexer:
    def __init__(
        self,
        store: SqliteStore,
        server_manager: ServerManager,
        embedding_client: EmbeddingProvider | None = None,
        timeout_sec: float = 60.0,
        concurrency: int = 4,
        aliases: AliasCatalog | None = None,
    ) -> None:
        self.store = store
        self.server_manager = server_manager
        self.embedding_client = embedding_client
        self.extractor = ToolExtractor(
            server_manager,
            timeout_sec=timeout_sec,
            concurrency=concurrency,
            aliases=aliases,
        )
        self.last_status = IndexStatus()

    async def rebuild_index(self) -> dict[str, Any]:
        self.store.connect()
        previous_count = self.store.tool_count()
        tools_meta, failed_servers = await self.extractor.extract_all_tools()
        tool_dicts = [t.model_dump() for t in tools_meta]

        if not tool_dicts and previous_count > 0:
            logger.warning(
                "No tools extracted (%s failed), keeping existing index (%s tools)",
                len(failed_servers),
                previous_count,
            )
            self.last_status = IndexStatus(
                status="ready",
                ok=True,
                tool_count=previous_count,
                embedding_ok=False,
                last_indexed_at=self.store.get_meta("last_indexed_at"),
                mode="keyword_only",
                failed_servers=failed_servers,
                error="partial: all index targets failed" if failed_servers else None,
            )
            return self.last_status.model_dump()

        embeddings: list[list[float]] | None = None
        embedding_ok = False
        if self.embedding_client and tool_dicts:
            try:
                texts = [t["text_content"] for t in tool_dicts]
                embeddings = []
                for i in range(0, len(texts), 32):
                    batch = texts[i : i + 32]
                    embeddings.extend(await self.embedding_client.embed_texts(batch))
                embedding_ok = len(embeddings) == len(tool_dicts)
            except Exception as exc:
                logger.warning("Embedding failed, indexing keyword-only: %s", exc)
                embeddings = None

        self.store.clear_index()
        count = self.store.upsert_tools(tool_dicts, embeddings)
        self.last_status = IndexStatus(
            status="ready",
            ok=True,
            tool_count=count,
            embedding_ok=embedding_ok,
            last_indexed_at=self.store.get_meta("last_indexed_at"),
            mode="hybrid" if embedding_ok else "keyword_only",
            failed_servers=failed_servers,
        )
        if failed_servers:
            logger.warning("Indexed %s tools; failed servers: %s", count, ", ".join(failed_servers))
        else:
            logger.info("Indexed %s tools (embedding_ok=%s)", count, embedding_ok)
        return self.last_status.model_dump()
