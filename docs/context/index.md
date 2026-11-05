---
title: Index rebuild
status: shipped
sources:
  - src/mcp_router/index/indexer.py
  - src/mcp_router/index/extractor.py
  - src/mcp_router/storage/sqlite_store.py
  - src/mcp_router/embedding/factory.py
  - src/mcp_router/embedding/types.py
  - src/mcp_router/embedding/local.py
  - src/mcp_router/embedding/openai_compatible.py
  - src/mcp_router/cli.py
related:
  - search.md
  - registry.md
---

# Index rebuild

`ToolIndexer.rebuild_index` replaces the SQLite tool index used by hybrid
search. Triggered by `mcp-router index-tools`, startup reindex, or
`POST /admin/reindex`.

## Steps

1. `ToolExtractor` calls `tools/list` on each registered server (bounded
   concurrency + per-server timeout). Builds `ToolMetadata` and
   `text_content` (description + parameter names/docs) for FTS.
2. Optional alias enrichment on text content.
3. Embeddings via `EmbeddingProvider` from `embedding.factory` — remote
   OpenAI-compatible API when `EMBEDDING_URL` + `EMBEDDING_MODEL` are set,
   otherwise local fastembed.
4. `SqliteStore` drops/rebuilds vector + FTS tables and writes rows.
5. Failed servers are recorded on `IndexStatus` without aborting the whole
   rebuild.

## Storage leaf

`storage/sqlite_store.py` owns schema and vector/FTS queries. It must not
import services, index, or HTTP adapters.

## Boundaries

`index` may use services (server manager, aliases) and storage/embedding.
It must not import `http_server` or `cli`. The CLI calls the indexer
directly for offline rebuilds.
