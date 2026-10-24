"""The batch mock's settings (``MOCK_BATCH_``-prefixed).

The mock always reaches the ledger over HTTP: like the stream mock, it is a
reasoner with no plugin installed next to the ledger. ``MOCK_BATCH_LEDGER_URL``
is therefore required, and a blank one refuses to run rather than fall back
to an in-process ledger; ``MOCK_BATCH_LEDGER_KEY`` is the mock's key, one of
the ledger's ``DECISIONLEDGER_API_KEYS`` (``mock-batch-reasoner:<key>``).

Read lazily (by the CLI's ``run``), never at import, so the plugin and the
eval adapter import with no environment at all.
"""

from __future__ import annotations

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MockBatchSettings(BaseSettings):
    """Configuration of the mock's CLI.

    Attributes:
        ledger_url: The ledger's base URL (``MOCK_BATCH_LEDGER_URL``),
            required: blank refuses to run.
        ledger_key: The mock's API key on that ledger
            (``MOCK_BATCH_LEDGER_KEY``), sent as ``X-DecisionLedger-Key``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="MOCK_BATCH_",
        extra="ignore",
    )

    ledger_url: str = Field(default="", validate_default=True)
    ledger_key: SecretStr = SecretStr("")

    @field_validator("ledger_url")
    @classmethod
    def _ledger_url_is_set(cls, value: str) -> str:
        """A blank URL refuses to run: the mock never runs the ledger itself."""
        url = value.strip()
        if not url:
            msg = (
                "MOCK_BATCH_LEDGER_URL is blank: the batch mock submits to a "
                "ledger over HTTP (for example http://localhost:8000)"
            )
            raise ValueError(msg)
        if not url.startswith(("http://", "https://")):
            msg = f"MOCK_BATCH_LEDGER_URL must be an http(s) URL, got {url!r}"
            raise ValueError(msg)
        return url.rstrip("/")
