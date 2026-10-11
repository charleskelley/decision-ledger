"""Framework bootstrap — the single place infrastructure clients are built.

``build_framework_services`` turns a ``FrameworkSettings`` plus the
discovered reasoner plugins into one ``FrameworkServices`` object: Postgres
(with pgvector adapters), Elasticsearch, the embedding and cross-encoder
models, the hybrid retriever, the prompt registry, the LLM policy gate, the
bundle store, and the reasoner registry. The FastAPI lifespan
(``app/main.py``) and the eval harness driver both call it, so there is
exactly one description of how the framework is wired.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg
import structlog
from elasticsearch import Elasticsearch
from pgvector.psycopg import register_vector
from sentence_transformers import CrossEncoder, SentenceTransformer

from app.audit.schema import apply_canonical_schema
from app.audit.store import BundleStore
from app.gate.policy.gate import PolicyGate
from app.gate.policy.prompt_registry import YamlPromptRegistry
from app.llm.openai import OpenAILLMClient
from app.plugin import FrameworkServices
from app.plugins import build_registry
from app.retrieval.retriever import PolicyRetriever

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.plugin import ReasonerPlugin
    from app.settings import FrameworkSettings

log = structlog.get_logger(__name__)

_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
_CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


def build_framework_services(
    settings: FrameworkSettings,
    plugins: Sequence[ReasonerPlugin],
) -> FrameworkServices:
    """Construct every shared service the framework runs on.

    Applies the canonical DDL to Postgres before returning so the store and
    journal can write on a fresh database.

    Args:
        settings: Connection parameters and credentials.
        plugins: The reasoner plugins this deployment composes. Their
            registrations form the reasoner registry, their raw-event
            factories are handed to the bundle store for replay, and their
            prompt directories feed the prompt registry.

    Returns:
        A ``FrameworkServices`` whose ``close()`` releases the connections.

    Raises:
        ValueError: Two plugins ship a prompt template with the same
            ``template_id`` (the prompt registry rejects the collision).
    """
    pg_conn = psycopg.connect(settings.postgres_dsn)
    # The DDL creates the pgvector extension, so it must run before
    # register_vector (which looks the ``vector`` type up in pg_type).
    apply_canonical_schema(pg_conn)
    # Teach psycopg how to adapt numpy ndarrays to pgvector parameters; the
    # retriever's dense-search query passes the embedding via %s.
    register_vector(pg_conn)
    es = Elasticsearch(settings.elasticsearch_url)

    embed_model = SentenceTransformer(_EMBEDDING_MODEL)
    cross_encoder = CrossEncoder(_CROSS_ENCODER_MODEL)
    retriever = PolicyRetriever(
        pg_conn=pg_conn,
        es=es,
        model=embed_model,
        cross_encoder=cross_encoder,
    )

    prompt_registry = YamlPromptRegistry([p.prompts_dir for p in plugins])
    gate = PolicyGate(client=OpenAILLMClient(), prompt_registry=prompt_registry)

    registry = build_registry(plugins)
    store = BundleStore(
        conn=pg_conn,
        raw_event_factories={
            p.registration.reasoner_id: p.raw_event_factory for p in plugins
        },
        registry=registry,
    )

    log.info(
        "framework.bootstrap_complete",
        component="bootstrap",
        reasoner_ids=[p.registration.reasoner_id for p in plugins],
        prompt_templates=prompt_registry.registered_ids(),
    )
    return FrameworkServices(
        settings=settings,
        pg_conn=pg_conn,
        es=es,
        retriever=retriever,
        prompt_registry=prompt_registry,
        gate=gate,
        store=store,
        registry=registry,
    )
