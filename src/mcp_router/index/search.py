"""Hybrid tool search: vector + BM25 + RRF (+ optional shadow ranker)."""

import json
import logging
from typing import Any

from mcp_router.embedding.types import EmbeddingProvider
from mcp_router.services.aliases import AliasCatalog
from mcp_router.services.intent_mapping import (
    detect_intent_from_query,
    detect_servers_from_query,
    get_tools_for_intents,
)
from mcp_router.services.server_manager import ServerManager
from mcp_router.storage.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)


class ToolSearch:
    def __init__(
        self,
        store: SqliteStore,
        server_manager: ServerManager,
        embedding_client: EmbeddingProvider | None = None,
        rrf_k: int = 60,
        server_boost: float = 1.2,
        aliases: AliasCatalog | None = None,
        alias_boost: float = 1.35,
        shadow_ranker: bool = False,
    ):
        self.store = store
        self.server_manager = server_manager
        self.embedding_client = embedding_client
        self.rrf_k = rrf_k
        self.server_boost = server_boost
        self.aliases = aliases or AliasCatalog()
        self.alias_boost = alias_boost
        self.shadow_ranker = shadow_ranker

    async def search(
        self,
        query: str,
        limit: int = 10,
        vector_weight: float = 0.4,
        fulltext_weight: float = 0.6,
    ) -> list[dict[str, Any]]:
        baseline, _shadow = await self.search_with_shadow(
            query=query,
            limit=limit,
            vector_weight=vector_weight,
            fulltext_weight=fulltext_weight,
        )
        return baseline

    async def search_with_shadow(
        self,
        query: str,
        limit: int = 10,
        vector_weight: float = 0.4,
        fulltext_weight: float = 0.6,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
        self.store.connect()
        detected_servers = detect_servers_from_query(query)
        alias_servers, alias_tools = self.aliases.matches(query)

        vector_results: list[dict[str, Any]] = []
        if self.embedding_client:
            try:
                query_vec = (await self.embedding_client.embed_texts([query]))[0]
                vector_results = self.store.search_vector(query_vec, limit * 2)
            except Exception as exc:
                logger.debug("Vector search unavailable: %s", exc)

        fts_results = self.store.search_fts(query, limit * 2)

        if vector_results or fts_results:
            baseline = self._rrf_merge(
                vector_results,
                fts_results,
                detected_servers,
                alias_servers=alias_servers,
                alias_tools=alias_tools,
                apply_alias_boost=True,
            )[:limit]
            shadow: list[dict[str, Any]] | None = None
            if self.shadow_ranker:
                shadow = self._weighted_merge(
                    vector_results,
                    fts_results,
                    detected_servers,
                    vector_weight=vector_weight,
                    fulltext_weight=fulltext_weight,
                    alias_servers=alias_servers,
                    alias_tools=alias_tools,
                )[:limit]
            return baseline, shadow

        fallback = await self._keyword_fallback(query, limit)
        return fallback, None

    def _rrf_merge(
        self,
        vector_results: list[dict[str, Any]],
        fts_results: list[dict[str, Any]],
        detected_servers: list[str],
        *,
        alias_servers: set[str] | None = None,
        alias_tools: set[str] | None = None,
        apply_alias_boost: bool = True,
    ) -> list[dict[str, Any]]:
        scores: dict[str, float] = {}
        meta: dict[str, dict[str, Any]] = {}
        alias_servers = alias_servers or set()
        alias_tools = alias_tools or set()

        for rank, item in enumerate(vector_results, start=1):
            tid = item["tool_id"]
            scores[tid] = scores.get(tid, 0.0) + 1.0 / (self.rrf_k + rank)
            meta[tid] = item

        for rank, item in enumerate(fts_results, start=1):
            tid = item["tool_id"]
            scores[tid] = scores.get(tid, 0.0) + 1.0 / (self.rrf_k + rank)
            meta.setdefault(tid, item)

        for tid in scores:
            server = meta[tid].get("server_name", "")
            if server in detected_servers:
                scores[tid] *= self.server_boost
            if apply_alias_boost and (tid in alias_tools or server in alias_servers):
                scores[tid] *= self.alias_boost

        return self._ranked_results(scores, meta)

    def _weighted_merge(
        self,
        vector_results: list[dict[str, Any]],
        fts_results: list[dict[str, Any]],
        detected_servers: list[str],
        *,
        vector_weight: float,
        fulltext_weight: float,
        alias_servers: set[str],
        alias_tools: set[str],
    ) -> list[dict[str, Any]]:
        """Shadow ranker: weighted reciprocal ranks (not shown to the agent)."""
        scores: dict[str, float] = {}
        meta: dict[str, dict[str, Any]] = {}
        vw = max(0.0, vector_weight)
        fw = max(0.0, fulltext_weight)
        total = vw + fw
        if total <= 0:
            vw, fw = 0.4, 0.6
            total = 1.0
        vw, fw = vw / total, fw / total

        for rank, item in enumerate(vector_results, start=1):
            tid = item["tool_id"]
            scores[tid] = scores.get(tid, 0.0) + vw / (self.rrf_k + rank)
            meta[tid] = item

        for rank, item in enumerate(fts_results, start=1):
            tid = item["tool_id"]
            scores[tid] = scores.get(tid, 0.0) + fw / (self.rrf_k + rank)
            meta.setdefault(tid, item)

        for tid in scores:
            server = meta[tid].get("server_name", "")
            if server in detected_servers:
                scores[tid] *= self.server_boost
            if tid in alias_tools or server in alias_servers:
                scores[tid] *= self.alias_boost * 1.1

        return self._ranked_results(scores, meta)

    def _ranked_results(
        self, scores: dict[str, float], meta: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        results = []
        for tid, score in ranked:
            item = meta[tid]
            schema = item.get("tool_schema")
            if isinstance(schema, str):
                try:
                    schema = json.loads(schema)
                except json.JSONDecodeError:
                    schema = {}
            results.append(
                {
                    "tool_id": tid,
                    "server_name": item.get("server_name"),
                    "tool_name": item.get("tool_name"),
                    "description": item.get("description", ""),
                    "score": round(score, 4),
                    "tool_schema": schema,
                }
            )
        return results

    async def _keyword_fallback(self, query: str, limit: int) -> list[dict[str, Any]]:
        intents = detect_intent_from_query(query)
        candidates = get_tools_for_intents(intents)
        results: list[dict[str, Any]] = []

        for server_name, tool_name in candidates[:limit]:
            stored = self.store.get_tool(server_name, tool_name)
            if stored:
                results.append(
                    {
                        "tool_id": stored["tool_id"],
                        "server_name": server_name,
                        "tool_name": tool_name,
                        "description": stored.get("description", ""),
                        "score": 0.5,
                        "source": "intent_fallback",
                    }
                )
                continue
            # registry-only fallback
            cfg = self.server_manager.get_server_config(server_name)
            if cfg:
                results.append(
                    {
                        "tool_id": f"{server_name}:{tool_name}",
                        "server_name": server_name,
                        "tool_name": tool_name,
                        "description": cfg.description,
                        "score": 0.3,
                        "source": "registry_intent",
                    }
                )

        if results:
            return results[:limit]

        # last resort: substring match on indexed tools
        q = query.lower()
        matches = []
        for tool in self.store.list_all_tools():
            hay = f"{tool['server_name']} {tool['tool_name']} {tool.get('description', '')}".lower()
            if any(word in hay for word in q.split() if len(word) > 2):
                matches.append(
                    {
                        "tool_id": tool["tool_id"],
                        "server_name": tool["server_name"],
                        "tool_name": tool["tool_name"],
                        "description": tool.get("description", ""),
                        "score": 0.2,
                        "source": "keyword",
                    }
                )
        return matches[:limit]
