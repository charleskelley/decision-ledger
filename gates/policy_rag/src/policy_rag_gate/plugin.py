"""Policy gate plugin — published under ``decisionledger.gates`` as ``policy``.

``PLUGIN`` satisfies ``app.gates.GatePlugin``. The member's own
``pyproject.toml`` publishes it (``policy = "policy_rag_gate.plugin:PLUGIN"``),
so the framework discovers this gate exactly as it discovers any other and
never imports it (DR-28).

``build`` constructs everything the gate owns, so the framework bootstrap
builds none of it:

- a ``ReasonerProfile`` per composed reasoner plugin — the templates in its
  ``prompts_dir`` and the jurisdictions of its ``corpus_dir`` — which the
  gate authorizes every submission against;
- a ``YamlPromptRegistry`` over every composed reasoner's ``prompts_dir``
  (two reasoners shipping the same ``template_id`` fail the build);
- the asset root (``POLICY_RAG_GATE_ASSETS_ROOT``, DR-33) the gate admits a
  reasoner registered without a plugin from: when the framework asks it to
  validate that reasoner's binding (at startup, or when the registration is
  written over the API), the gate reads ``<root>/<reasoner_id>/prompts`` and
  ``corpus`` and adds the reasoner's templates and profile;
- its own Postgres connection (autocommit) to the framework's database, on
  which it applies its own DDL — the vector extension and the
  ``policy_rag`` schema with the chunk table — before registering the
  pgvector adapters, for the dense retrieval leg;
- the Elasticsearch client for the BM25 leg
  (``POLICY_RAG_GATE_ELASTICSEARCH_URL``);
- the embedding and cross-encoder models and the hybrid ``PolicyRetriever``;
- the OpenAI-backed ``LLMClient``, with the settings' ``OPENAI_API_KEY``.

Building opens no Elasticsearch connection (the client connects lazily), so
a deployment whose decisions never route to this gate runs with
Elasticsearch down.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg
import structlog
from elasticsearch import Elasticsearch
from pgvector.psycopg import register_vector
from sentence_transformers import CrossEncoder, SentenceTransformer

from app.gates import GateKind
from policy_rag_gate.config import PolicyGateConfig, ReasonerProfile
from policy_rag_gate.gate import POLICY_GATE_ID, PolicyGate
from policy_rag_gate.llm.openai import OpenAILLMClient
from policy_rag_gate.prompt_registry import YamlPromptRegistry
from policy_rag_gate.retriever import PolicyRetriever
from policy_rag_gate.schema import apply_schema
from policy_rag_gate.settings import PolicyRagGateSettings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic import BaseModel

    from app.plugin import FrameworkServices, ReasonerPlugin

log = structlog.get_logger(__name__)

#: Sentence-transformer used to embed queries (must match the corpus loader).
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
#: Cross-encoder used to rerank fused candidates.
CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


def reasoner_profiles(
    reasoners: Sequence[ReasonerPlugin],
) -> dict[str, ReasonerProfile]:
    """Derive what each reasoner may submit from the assets it ships.

    Args:
        reasoners: The composed reasoner plugins.

    Returns:
        One ``ReasonerProfile`` per reasoner, keyed by ``reasoner_id``.

    Raises:
        ValueError: A reasoner's prompt template file is malformed.
    """
    return {
        plugin.registration.reasoner_id: ReasonerProfile.from_assets(
            prompts_dir=plugin.prompts_dir, corpus_dir=plugin.corpus_dir
        )
        for plugin in reasoners
    }


def open_retriever(
    *, postgres_dsn: str, elasticsearch_url: str
) -> tuple[PolicyRetriever, tuple[Elasticsearch, psycopg.Connection]]:
    """Open the gate's stores and models and build the hybrid retriever.

    Applies the gate's own DDL first, so a fresh database gets the vector
    extension and the ``policy_rag`` schema before the pgvector adapters are
    registered.

    Args:
        postgres_dsn: DSN of the database that holds the ``policy_rag``
            schema (the framework's database; the gate owns the schema).
        elasticsearch_url: URL of the gate's Elasticsearch.

    Returns:
        The retriever and the handles to close when done, in closing order:
        the Elasticsearch client, then the autocommit Postgres connection. On
        failure the connection is closed before the error propagates.
    """
    pg_conn = psycopg.connect(postgres_dsn, autocommit=True)
    try:
        apply_schema(pg_conn)
        register_vector(pg_conn)
        es = Elasticsearch(elasticsearch_url)
        retriever = PolicyRetriever(
            pg_conn=pg_conn,
            es=es,
            model=SentenceTransformer(EMBEDDING_MODEL),
            cross_encoder=CrossEncoder(CROSS_ENCODER_MODEL),
        )
    except Exception:
        pg_conn.close()
        raise
    return retriever, (es, pg_conn)


class PolicyGatePlugin:
    """``GatePlugin`` for the LLM-backed policy gate.

    Args:
        settings: Optional explicit ``PolicyRagGateSettings``. When omitted,
            settings are read from the environment the first time ``build``
            runs, so importing the plugin never touches ``.env``.

    Attributes:
        gate_id: ``"policy"``.
        kind: ``GateKind.LLM``.
        deterministic: ``False`` — an LLM verdict is not a pure function of
            its input, so enforcement applies Tiers 2 and 4.
        config_model: ``PolicyGateConfig``, validated by the framework at
            intake.
        binding_config_model: ``None`` — the gate takes no binding
            configuration; the built gate's ``validate_binding`` admits
            each binding (DR-33).
    """

    gate_id: str = POLICY_GATE_ID
    kind: GateKind = GateKind.LLM
    deterministic: bool = False
    config_model: type[BaseModel] | None = PolicyGateConfig
    binding_config_model: type[BaseModel] | None = None

    def __init__(self, settings: PolicyRagGateSettings | None = None) -> None:
        """Bind the plugin; defer reading settings to build time."""
        self._settings = settings

    @property
    def settings(self) -> PolicyRagGateSettings:
        """The settings ``build`` will use (read lazily)."""
        if self._settings is None:
            self._settings = PolicyRagGateSettings()
        return self._settings

    def build(self, framework: FrameworkServices) -> PolicyGate:
        """Build the policy gate and the infrastructure it owns.

        Args:
            framework: Shared services. The gate reads ``settings`` (the
                Postgres DSN of the database that holds its schema) and
                ``reasoners`` (each reasoner's ``prompts_dir`` and
                ``corpus_dir``). A reasoner registered without a plugin is
                admitted later, from the asset root, when the framework
                asks the gate to validate its binding.

        Returns:
            The runtime ``PolicyGate``; ``close()`` releases its Postgres
            connection and Elasticsearch client.

        Raises:
            ValueError: Two composed reasoners ship a prompt template with
                the same ``template_id``, or a template file is malformed.
        """
        settings = self.settings
        prompt_registry = YamlPromptRegistry(
            [p.prompts_dir for p in framework.reasoners]
        )
        profiles = reasoner_profiles(framework.reasoners)

        retriever, resources = open_retriever(
            postgres_dsn=framework.settings.postgres_dsn,
            elasticsearch_url=settings.elasticsearch_url,
        )
        try:
            client = OpenAILLMClient(api_key=settings.openai_api_key or None)
        except Exception:
            for resource in resources:
                resource.close()
            raise

        log.info(
            "policy_gate.built",
            component="policy_gate",
            prompt_templates=prompt_registry.registered_ids(),
            reasoner_ids=sorted(profiles),
            assets_root=str(settings.assets_root_path or ""),
            embedding_model=EMBEDDING_MODEL,
            cross_encoder_model=CROSS_ENCODER_MODEL,
        )
        return PolicyGate(
            client=client,
            prompt_registry=prompt_registry,
            retriever=retriever,
            profiles=profiles,
            resources=resources,
            assets_root=settings.assets_root_path,
        )


#: The entry-point target: ``policy = "policy_rag_gate.plugin:PLUGIN"``.
PLUGIN = PolicyGatePlugin()
