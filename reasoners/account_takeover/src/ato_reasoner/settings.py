"""ATO Reasoner configuration — domain-specific settings.

``AtoSettings`` holds configuration that is unique to the Account Takeover
reasoner: the Redis instance that backs its sliding-window features and
login-event stream, the trained scorer model artifact path, and the active
policy corpus version. Shared infrastructure (Postgres, Elasticsearch, LLM
credentials, logging) is owned by ``app/settings.py:FrameworkSettings`` and
reaches the reasoner through ``FrameworkServices``.

Environment variables are read from the same ``.env`` as the framework,
but each ATO-specific variable is prefixed ``ATO_`` so it never collides
with a framework setting or another reasoner's settings.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_PACKAGE_DIR = Path(__file__).resolve().parent

#: Default trained-model artifact, resolved relative to this package so the
#: path is valid regardless of the process's working directory.
DEFAULT_SCORER_MODEL_PATH = _PACKAGE_DIR / "scorer" / "models" / "ato-v1.ubj"


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

    #: Redis backing the feature windows, dedup store, and login-event stream.
    redis_url: str = "redis://localhost:6379"
    scorer_model_path: str = str(DEFAULT_SCORER_MODEL_PATH)
    corpus_version: str = "ato-v1"
