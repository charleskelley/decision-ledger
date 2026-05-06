"""Framework-runtime configuration (shared infrastructure).

``FrameworkSettings`` holds configuration for resources that are shared
across the framework runtime and any registered reasoners — Postgres,
Redis, Elasticsearch, LLM credentials, and process-wide logging. Tenant
separation is logical (Postgres schemas, Redis key prefixes, ES indices),
not connection-level — multiple reasoners share one deployment of each
resource.

Per-reasoner configuration (e.g., model artifact paths, corpus versions)
lives in that reasoner's own ``Settings`` class with a domain-specific
env-var prefix. See ``reasoner/account_takeover/settings.py:AtoSettings``
for the reference implementation.

Values are resolved in this precedence order (Pydantic Settings default):

1. Constructor kwargs (used by tests for explicit overrides).
2. Process environment variables.
3. Values loaded from ``.env`` at the project root.
4. Field defaults defined below.

The ``.env`` file is gitignored. ``.env.example`` (committed) documents
the contract. See ``docs/operations/secrets.md`` for full guidance.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class FrameworkSettings(BaseSettings):
    """Shared-infrastructure configuration with docker-compose defaults.

    Owns Postgres / Redis / Elasticsearch connection params, LLM
    credentials, and logging knobs. These are deployment-level concerns —
    the framework's lifespan composer constructs the corresponding clients
    from this object and injects them into reasoner services.

    Reasoner-specific configuration must NOT be added here; create a
    per-reasoner Settings class with a domain-specific env-var prefix.
    """

    # ``extra="ignore"`` tolerates additional vars in ``.env`` that don't
    # map to a Settings field (e.g., reasoner-prefixed vars consumed by a
    # peer Settings class, direnv-only project paths). Without it, a stray
    # entry would crash construction.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "decisionledger"
    postgres_password: str = "decisionledger"  # noqa: S105
    postgres_db: str = "decisionledger"

    redis_host: str = "localhost"
    redis_port: int = 6379

    elasticsearch_url: str = "http://localhost:9200"

    openai_api_key: str = ""
    anthropic_api_key: str = ""

    log_json: bool = True
    log_level: str = "INFO"

    @property
    def postgres_dsn(self) -> str:
        """Build a PostgreSQL connection string from components."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
