"""RAG policy gate configuration — the gate's own infrastructure settings.

``PolicyRagGateSettings`` reads ``POLICY_RAG_GATE_*`` variables from the
process environment or the project ``.env`` (the same file the framework
reads; the prefix keeps the gate's variables from colliding with anyone
else's). The gate's Elasticsearch is its own infrastructure (DR-28), so its
URL lives here rather than in the framework settings.

The LLM provider keys keep their providers' standard names
(``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``): the SDKs read those variables
themselves, and the gate reads them only to fail fast with a clear message.

The gate's Postgres tables live in the framework's database (in the gate's
own ``policy_rag`` schema), so the plugin connects with the framework's DSN
and the CLIs read it from ``FrameworkSettings``.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PolicyRagGateSettings(BaseSettings):
    """RAG policy gate settings.

    Attributes:
        elasticsearch_url: URL of the gate's Elasticsearch
            (``POLICY_RAG_GATE_ELASTICSEARCH_URL``; default matches the
            member's ``compose.yaml``).
        openai_api_key: ``OPENAI_API_KEY`` — the gate's LLM client and the
            eval's faithfulness scorer.
        anthropic_api_key: ``ANTHROPIC_API_KEY`` — the eval's LLM judges.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="POLICY_RAG_GATE_",
        extra="ignore",
        populate_by_name=True,
    )

    elasticsearch_url: str = "http://localhost:9200"
    openai_api_key: str = Field(default="", validation_alias="OPENAI_API_KEY")
    anthropic_api_key: str = Field(default="", validation_alias="ANTHROPIC_API_KEY")
