"""Application settings (environment-backed)."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_LOCAL_EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


class Settings(BaseSettings):
    """Runtime settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    registry_path: Path = Path("config/registry.json")
    index_db_path: Path = Path("data/tools.db")
    aliases_path: Path = Path("config/aliases.json")

    embedding_url: str = ""
    embedding_model: str = ""
    embedding_api_key: str = ""
    local_embedding_model: str = DEFAULT_LOCAL_EMBEDDING_MODEL

    reindex_on_start: bool = True
    reindex_background: bool = True
    index_tools_timeout_sec: float = 60.0
    index_concurrency: int = 4

    # Hot-reload registry.json without process restart (PLAN U7)
    registry_hot_reload: bool = True
    registry_poll_interval_sec: float = Field(default=2.0, ge=0.1)

    health_probe_enabled: bool = True
    health_probe_interval_sec: float = Field(default=60.0, ge=1.0)
    health_probe_timeout_sec: float = Field(default=15.0, gt=0)
    health_probe_concurrency: int = Field(default=4, ge=1)
    # Day-path: hide/block known-unhealthy servers (WOR-1092). Rollback: false.
    health_quarantine_enabled: bool = True

    mcp_router_host: str = "0.0.0.0"
    mcp_router_port: int = 8000
    router_api_key: str = ""
    # Comma-separated CORS origins (env ALLOWED_ORIGINS). Default "*" for LAN.
    allowed_origins: str = "*"

    cache_ttl_seconds: int = 1800
    rrf_k: int = Field(default=60, ge=1)
    server_boost: float = Field(default=1.2, gt=0)
    search_max_per_server: int = Field(default=2, ge=1)
    alias_boost: float = Field(default=1.35, gt=0)
    shadow_ranker: bool = False
    search_outcome_window_sec: int = Field(default=600, ge=1)

    # Strict / dual MCP surface (default /mcp unchanged; /mcp/v2 enforces policy)
    strict_mcp_enabled: bool = True
    strict_allowlist: str = ""
    strict_read_only: bool = False

    @property
    def cors_allow_origins(self) -> list[str]:
        origins = [o.strip() for o in self.allowed_origins.split(",") if o.strip()]
        return origins if origins else ["*"]

    @property
    def use_remote_embeddings(self) -> bool:
        return bool(self.embedding_url.strip() and self.embedding_model.strip())

    @property
    def embedding_mode(self) -> str:
        return "remote" if self.use_remote_embeddings else "local"

    @property
    def embedding_available(self) -> bool:
        return self.use_remote_embeddings or bool(self.local_embedding_model.strip())


def get_settings() -> Settings:
    return Settings()
