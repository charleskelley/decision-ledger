"""Environment-driven application configuration.

Single ``Settings`` source of truth shared across ``app/main.py`` (FastAPI
lifespan) and ``eval/clients/pipeline.py`` (eval harness driver). All
fields default to docker-compose-local values.

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


class Settings(BaseSettings):
    """Environment-driven configuration with docker-compose defaults."""

    # ``extra="ignore"`` tolerates additional vars in ``.env`` that don't
    # map to a Settings field (e.g., direnv-only project paths). Without
    # it, a stray entry would crash construction.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "account_takeover"
    postgres_password: str = "account_takeover"  # noqa: S105
    postgres_db: str = "account_takeover"

    redis_host: str = "localhost"
    redis_port: int = 6379

    elasticsearch_url: str = "http://localhost:9200"

    openai_api_key: str = ""
    anthropic_api_key: str = ""

    scorer_model_path: str = "app/scorer/models/ato-v1.ubj"

    corpus_version: str = "unknown"

    log_json: bool = True
    log_level: str = "INFO"

    @property
    def postgres_dsn(self) -> str:
        """Build a PostgreSQL connection string from components."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
