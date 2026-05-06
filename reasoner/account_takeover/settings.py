"""ATO Reasoner configuration — domain-specific settings.

``AtoSettings`` holds configuration that is unique to the Account Takeover
reasoner: the trained scorer model artifact path and the active policy
corpus version. Shared infrastructure (Postgres, Redis, Elasticsearch,
LLM credentials, logging) is owned by ``app/settings.py:FrameworkSettings``
and injected into reasoner services by the deployment composer.

Environment variables are read from the same ``.env`` as the framework,
but each ATO-specific variable is prefixed ``ATO_`` so it never collides
with a framework setting or another reasoner's settings.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class AtoSettings(BaseSettings):
    """ATO reasoner configuration with docker-compose defaults.

    Reads ``ATO_*`` environment variables from the project ``.env`` file
    or process environment. Reasoner code receives an instance of this
    class via constructor injection — it never reads ``os.environ``
    directly.
    """

    # Same env_file as FrameworkSettings — single .env source of truth.
    # extra="ignore" lets us coexist with framework and peer-reasoner vars.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="ATO_",
        extra="ignore",
    )

    scorer_model_path: str = "reasoner/account_takeover/scorer/models/ato-v1.ubj"
    corpus_version: str = "ato-v1"
