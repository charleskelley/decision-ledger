"""Framework-runtime configuration (shared infrastructure).

``FrameworkSettings`` holds configuration for resources that are shared
across the framework runtime and every plugin it composes — Postgres (the
audit ledger, the framework's only infrastructure), process-wide logging,
the reasoner allowlist, the registration file, the gates the deployment
mandates for every reasoner, the active identity resolver, and the API keys
the ledger authenticates callers with (keys live in the environment or a
mounted file only; there is no user model and no session). Tenant
separation is logical (the ``reasoner_id`` column), not connection-level —
multiple reasoners share one deployment.

Per-plugin configuration lives in that plugin's own ``Settings`` class with
its own env-var prefix, inside its package: a reasoner's model artifact
paths, corpus version, or queue; a gate's search index or model endpoint
(DR-28). A gate that keeps tables in the framework's database (in its own
schema) reads the Postgres DSN from here.

Values are resolved in this precedence order (Pydantic Settings default):

1. Constructor kwargs (used by tests for explicit overrides).
2. Process environment variables.
3. Values loaded from ``.env`` at the project root.
4. Field defaults defined below.

The ``.env`` file is gitignored. ``.env.example`` (committed) documents
the contract. See ``docs/operations/secrets.md`` for full guidance.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class FrameworkSettings(BaseSettings):
    """Shared-infrastructure configuration with docker-compose defaults.

    Owns the Postgres connection params, logging knobs, the reasoner
    allowlist and registration file, the deployment's mandatory gates, and
    the API keys — ``app/bootstrap.py`` opens the framework's Postgres
    connection and composes the registrations and gates from them, and
    ``app/auth.py`` builds the key ring.

    Reasoner- or gate-specific configuration must NOT be added here; create
    a Settings class with that plugin's own env-var prefix in its package.
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

    log_json: bool = True
    log_level: str = "INFO"

    #: Comma-separated allowlist of ``reasoner_id`` values to compose from the
    #: installed plugins. Blank (the default) composes every installed plugin.
    decisionledger_reasoners: str = ""

    #: Comma-separated ``gate_id`` values the deployment runs on every
    #: decision of every reasoner, recorded or gated, in addition to each
    #: registration's own gate set (DR-29). Each must be composed, or startup
    #: fails. Blank (the default) mandates none.
    decisionledger_mandatory_gates: str = ""

    #: Path to a YAML file of reasoner registrations, composed with the
    #: installed reasoner plugins' (DR-31): how a reasoner that runs as its
    #: own service, with no plugin installed here, registers. Blank (the
    #: default) reads no file.
    decisionledger_registrations: str = ""

    #: The deployment's one active identity resolver (DR-32): the name of a
    #: plugin installed under the ``decisionledger.identity_resolvers``
    #: entry-point group, or blank (or ``passthrough``) for the passthrough
    #: default, which records the subject each reasoner names, unresolved. An
    #: installed resolver is never activated implicitly; a name that is not
    #: installed refuses startup.
    decisionledger_identity_resolver: str = ""

    #: API keys of the reasoners, as comma-separated ``reasoner_id:key``
    #: pairs. A key authenticates its reasoner's writes and its reads of its
    #: own decisions; a reasoner may hold several keys (rotation).
    decisionledger_api_keys: SecretStr = SecretStr("")

    #: Path to a YAML file mapping ``reasoner_id`` to a key or a list of keys,
    #: read in addition to ``DECISIONLEDGER_API_KEYS`` (a mounted secret).
    decisionledger_api_keys_file: str = ""

    #: The operator key: a single shared secret that resolves held decisions
    #: and reads every reasoner's decisions. Blank disables operator access.
    decisionledger_operator_key: SecretStr = SecretStr("")

    @property
    def mandatory_gate_ids(self) -> frozenset[str]:
        """The parsed ``DECISIONLEDGER_MANDATORY_GATES`` set (empty when blank)."""
        return frozenset(
            part.strip()
            for part in self.decisionledger_mandatory_gates.split(",")
            if part.strip()
        )

    @property
    def identity_resolver_id(self) -> str | None:
        """The active resolver's id, or ``None`` for the passthrough default."""
        value = self.decisionledger_identity_resolver.strip()
        return None if value in ("", "passthrough") else value

    @property
    def registrations_path(self) -> Path | None:
        """The ``DECISIONLEDGER_REGISTRATIONS`` file, or ``None`` when blank."""
        value = self.decisionledger_registrations.strip()
        return Path(value) if value else None

    @property
    def api_keys_path(self) -> Path | None:
        """The ``DECISIONLEDGER_API_KEYS_FILE`` file, or ``None`` when blank."""
        value = self.decisionledger_api_keys_file.strip()
        return Path(value) if value else None

    @property
    def postgres_dsn(self) -> str:
        """Build a PostgreSQL connection string from components."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
