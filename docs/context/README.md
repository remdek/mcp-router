# mcp-router — design fragments

Small, focused fragments that mirror the codebase. Each declares the source files
it covers via frontmatter `sources:`. When those files change, update the matching
fragment (or run `python scripts/context_drift.py` to list which ones to review).

Import boundaries are enforced by [import-linter](import-boundaries.md)
(`make lint-imports`).

| Fragment | Covers |
|---|---|
| [Hybrid search](search.md) | `index/search.py`, aliases, intent mapping, telemetry |
| [Proxy call](proxy-call.md) | `call_server_tool`, server manager, transports, remediation |
| [Registry](registry.md) | registry JSON load, env substitution, models |
| [Index rebuild](index.md) | extractor, indexer, SQLite store, embeddings |
| [Import boundaries](import-boundaries.md) | layer contracts in `pyproject.toml` |
