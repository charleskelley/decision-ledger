"""The stream mock's settings (``MOCK_STREAM_``-prefixed).

The mock always reaches the ledger over HTTP: it is the reference pattern
for a reasoner that runs as its own service, with no plugin installed next to
the ledger. ``MOCK_STREAM_LEDGER_URL`` is therefore required, and a blank one
refuses to start rather than fall back to an in-process ledger;
``MOCK_STREAM_LEDGER_KEY`` is the mock's key, one of the ledger's
``DECISIONLEDGER_API_KEYS`` (``mock-stream-reasoner:<key>``).

Read lazily (by the service's lifespan and the CLI), never at import, so the
plugin and the eval adapter import with no environment at all.
"""

from __future__ import annotations

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from mock_stream_reasoner.pipeline import HISTORY
from mock_stream_reasoner.source import ALLOWED_LATENESS
from mock_stream_reasoner.state import WINDOW


class MockStreamSettings(BaseSettings):
    """Configuration of the mock's service and CLI.

    Attributes:
        ledger_url: The ledger's base URL (``MOCK_STREAM_LEDGER_URL``),
            required: blank refuses to start.
        ledger_key: The mock's API key on that ledger
            (``MOCK_STREAM_LEDGER_KEY``), sent as ``X-DecisionLedger-Key``.
        window_seconds: How far back a transfer's per-account window reaches.
        allowed_lateness_seconds: How far behind the stream's watermark an
            event may arrive and still be decided.
        history: How many recent decisions the service lists
            (``GET /api/v1/stream/decisions``); the ledger holds them all.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="MOCK_STREAM_",
        extra="ignore",
    )

    ledger_url: str = Field(default="", validate_default=True)
    ledger_key: SecretStr = SecretStr("")
    window_seconds: int = Field(default=int(WINDOW.total_seconds()), gt=0)
    allowed_lateness_seconds: int = Field(
        default=int(ALLOWED_LATENESS.total_seconds()), ge=0
    )
    history: int = Field(default=HISTORY, ge=1, le=100_000)

    @field_validator("ledger_url")
    @classmethod
    def _ledger_url_is_set(cls, value: str) -> str:
        """A blank URL refuses to start: the mock never runs the ledger itself."""
        url = value.strip()
        if not url:
            msg = (
                "MOCK_STREAM_LEDGER_URL is blank: the stream mock submits to a "
                "ledger over HTTP (for example http://localhost:8000)"
            )
            raise ValueError(msg)
        if not url.startswith(("http://", "https://")):
            msg = f"MOCK_STREAM_LEDGER_URL must be an http(s) URL, got {url!r}"
            raise ValueError(msg)
        return url.rstrip("/")
