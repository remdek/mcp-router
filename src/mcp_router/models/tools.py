"""Tool index and search models."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolMetadata(BaseModel):
    tool_id: str
    server_name: str
    tool_name: str
    description: str = ""
    text_content: str
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    tool_schema: dict[str, Any] = Field(default_factory=dict)


class ToolSearchResult(BaseModel):
    tool_id: str
    server_name: str
    tool_name: str
    description: str = ""
    score: float = 0.0
    tool_schema: dict[str, Any] | None = None
    schema_omitted: bool | None = None
    source: str | None = None


class ServerInfo(BaseModel):
    name: str
    description: str = ""
    type: str
    status: Literal["registered"] = "registered"


class IndexStatus(BaseModel):
    status: Literal["pending", "indexing", "ready", "error"] = "pending"
    ok: bool = False
    tool_count: int = 0
    embedding_ok: bool = False
    last_indexed_at: str | None = None
    mode: Literal["hybrid", "keyword_only"] | None = None
    error: str | None = None
    failed_servers: list[str] = Field(default_factory=list)
