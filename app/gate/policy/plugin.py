"""Policy gate plugin — published under ``decisionledger.gates`` as ``policy``.

``PLUGIN`` satisfies ``app.gates.GatePlugin``. While the gate's code still
lives in ``app/``, the framework's root package publishes it through an
interim entry point (``policy = "app.gate.policy.plugin:PLUGIN"``), exactly as
DR-26 Phase 1 registered the first reasoner before it moved; Phase 1b moves
the gate into ``gates/policy_rag/`` and the entry point with it (DR-28).

``build`` constructs everything the gate owns, so the framework bootstrap
builds none of it:

- its own Postgres connection (autocommit, pgvector adapters registered) for
  the dense retrieval leg;
- the Elasticsearch client for the BM25 leg;
- the embedding and cross-encoder models and the hybrid ``PolicyRetriever``;
- a ``YamlPromptRegistry`` over every composed reasoner's ``prompts_dir``
  (two reasoners shipping the same ``template_id`` fail the build);
- the OpenAI-backed ``LLMClient`` (reads ``OPENAI_API_KEY``).

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

from app.gate.policy.gate import POLICY_GATE_ID, PolicyGate
from app.gate.policy.prompt_registry import YamlPromptRegistry
from app.gates import GateKind
from app.llm.openai import OpenAILLMClient
from app.retrieval.retriever import PolicyRetriever

if TYPE_CHECKING:
    from pydantic import BaseModel

    from app.plugin import FrameworkServices

log = structlog.get_logger(__name__)

#: Sentence-transformer used to embed queries (must match the corpus loader).
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
#: Cross-encoder used to rerank fused candidates.
CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


class PolicyGatePlugin:
    """``GatePlugin`` for the LLM-backed policy gate.

    Attributes:
        gate_id: ``"policy"``.
        kind: ``GateKind.LLM``.
        deterministic: ``False`` — an LLM verdict is not a pure function of
            its input, so enforcement applies Tiers 2 and 4.
        config_model: ``None``. The gate validates its ``gate_config`` keys
            on consumption; a typed config model arrives when the gate moves
            into its own member (Phase 1b).
    """

    gate_id: str = POLICY_GATE_ID
    kind: GateKind = GateKind.LLM
    deterministic: bool = False
    config_model: type[BaseModel] | None = None

    def build(self, framework: FrameworkServices) -> PolicyGate:
        """Build the policy gate and the infrastructure it owns.

        Args:
            framework: Shared services. The gate reads ``settings`` (the
                Postgres DSN, the Elasticsearch URL) and ``reasoners`` (each
                reasoner's ``prompts_dir``).

        Returns:
            The runtime ``PolicyGate``; ``close()`` releases its Postgres
            connection and Elasticsearch client.

        Raises:
            ValueError: Two composed reasoners ship a prompt template with
                the same ``template_id``.
        """
        settings = framework.settings
        prompt_registry = YamlPromptRegistry(
            [p.prompts_dir for p in framework.reasoners]
        )

        pg_conn = psycopg.connect(settings.postgres_dsn, autocommit=True)
        try:
            # The framework DDL (applied before gates are built) creates the
            # pgvector extension; register_vector looks the type up.
            register_vector(pg_conn)
            es = Elasticsearch(settings.elasticsearch_url)
            retriever = PolicyRetriever(
                pg_conn=pg_conn,
                es=es,
                model=SentenceTransformer(EMBEDDING_MODEL),
                cross_encoder=CrossEncoder(CROSS_ENCODER_MODEL),
            )
            client = OpenAILLMClient()
        except Exception:
            pg_conn.close()
            raise

        log.info(
            "policy_gate.built",
            component="policy_gate",
            prompt_templates=prompt_registry.registered_ids(),
            embedding_model=EMBEDDING_MODEL,
            cross_encoder_model=CROSS_ENCODER_MODEL,
        )
        return PolicyGate(
            client=client,
            prompt_registry=prompt_registry,
            retriever=retriever,
            resources=(es, pg_conn),
        )


#: The entry-point target: ``policy = "app.gate.policy.plugin:PLUGIN"``.
PLUGIN = PolicyGatePlugin()
