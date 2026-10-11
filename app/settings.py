"""Framework-runtime configuration (shared infrastructure).

``FrameworkSettings`` holds configuration for resources that are shared
across the framework runtime and every reasoner plugin it composes —
Postgres, Elasticsearch, LLM credentials, process-wide logging, and the
reasoner allowlist. Tenant separation is logical (the ``reasoner_id``
column, per-reasoner ES documents), not connection-level — multiple
reasoners share one deployment of each resource.

Per-reasoner configuration (model artifact paths, corpus versions, a
reasoner's own queue or cache) lives in that reasoner's own ``Settings``
class with a domain-specific env-var prefix, inside the reasoner's package.

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

    Owns Postgres / Elasticsearch connection params, LLM credentials,
    logging knobs, and the reasoner allowlist. These are deployment-level
    concerns — ``app/bootstrap.py`` constructs the corresponding clients
    from this object and hands them to each reasoner plugin.

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

    elasticsearch_url: str = "http://localhost:9200"

    openai_api_key: str = ""
    anthropic_api_key: str = ""

    log_json: bool = True
    log_level: str = "INFO"

    #: Comma-separated allowlist of ``reasoner_id`` values to compose from the
    #: installed plugins. Blank (the default) composes every installed plugin.
    decisionledger_reasoners: str = ""

    @property
    def postgres_dsn(self) -> str:
        """Build a PostgreSQL connection string from components."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
