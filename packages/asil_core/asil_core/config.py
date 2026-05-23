"""Process-wide configuration loaded from environment.

Read once at import time via `settings = Settings()`. Subsystems should never
read os.environ directly — they consume Settings.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProfileName = Literal["tight", "balanced", "generous"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- LLM routing ----
    asil_llm_profile: LLMProfileName = "tight"
    asil_daily_budget_usd: float | None = Field(default=2.0, ge=0.0)

    # ---- Provider keys (optional — only required for the active profile) ----
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    deepseek_api_key: str | None = None
    voyage_api_key: str | None = None

    asil_embed_endpoint: str = "http://localhost:8001/embed"

    # ---- Github ----
    github_token: str | None = None

    # ---- Datastores ----
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "asil_dev_password"

    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None

    postgres_dsn: str = "postgresql+asyncpg://asil:asil_dev_password@localhost:5432/asil"
    redis_url: str = "redis://localhost:6379/0"

    # ---- Observability ----
    loki_url: str = "http://localhost:3100"
    prometheus_url: str = "http://localhost:9090"
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"
    otel_service_name: str = "asil"

    # ---- App ----
    asil_log_level: str = "INFO"
    asil_env: Literal["dev", "test", "prod"] = "dev"

    # ---- Ingestion ----
    # Where cloned repos and per-repo caches live. Defaults to .asil_cache/
    # inside the current working directory (matching .gitignore).
    asil_cache_dir: str = ".asil_cache"


_settings: Settings | None = None


def get_settings() -> Settings:
    """Lazy singleton — avoid loading env at import time of every module."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
