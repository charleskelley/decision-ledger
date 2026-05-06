"""FastAPI deployment composer — wires framework + reasoner services.

Constructs all infrastructure clients and reasoner-specific services in
the lifespan context manager, then mounts each registered reasoner's
router. This file is the single sanctioned ``app/`` → ``reasoner.*``
import seam — every other framework module is reasoner-agnostic.

Generic framework routes (decision lookup, replay) live here since they
key on ``decision_id`` only. ATO-specific routes (``POST /api/v1/ato/
decisions``) live in ``reasoner/account_takeover/api.py``.

Run locally::

    uv run uvicorn app.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
import redis
import structlog
from elasticsearch import Elasticsearch
from fastapi import FastAPI, HTTPException, Request
from pgvector.psycopg import register_vector
from pydantic import BaseModel
from sentence_transformers import CrossEncoder, SentenceTransformer

from app.audit import BundleStore
from app.gate.policy import PolicyGate, YamlPromptRegistry
from app.llm.openai import OpenAILLMClient
from app.monitoring import configure_logging
from app.retrieval.retriever import PolicyRetriever
from app.settings import FrameworkSettings
from reasoner.account_takeover.api import router as ato_router
from reasoner.account_takeover.events import LoginEvent
from reasoner.account_takeover.feature_service import FeatureService
from reasoner.account_takeover.scorer import AtoScorer
from reasoner.account_takeover.settings import AtoSettings

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Framework-side response models (generic — keyed on decision_id)
# ---------------------------------------------------------------------------


class ReplayResponse(BaseModel):
    """Result of replaying enforcement against cached gate output."""

    decision_id: str
    original_action: str
    replayed_action: str
    actions_match: bool


class ErrorResponse(BaseModel):
    """Consistent error envelope."""

    error_code: str
    message: str
    details: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Lifespan — service construction and teardown
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Construct all framework + reasoner services at startup."""
    fw = FrameworkSettings()
    ato = AtoSettings()
    configure_logging(json_logs=fw.log_json, level=fw.log_level)

    model_path = Path(ato.scorer_model_path)
    if not model_path.exists():
        msg = (
            f"Scorer model not found at {model_path}. "
            "Run `make train` or "
            "`uv run python -m reasoner.account_takeover.scorer train "
            f"--output {model_path} --samples 2000` first."
        )
        raise FileNotFoundError(msg)

    redis_client = redis.Redis(
        host=fw.redis_host,
        port=fw.redis_port,
        decode_responses=False,
    )
    pg_conn = psycopg.connect(fw.postgres_dsn)
    # Teach psycopg how to adapt numpy ndarrays to pgvector parameters; the
    # retriever's dense-search query passes the embedding via %s.
    register_vector(pg_conn)
    es_client = Elasticsearch(fw.elasticsearch_url)

    embed_model = SentenceTransformer("all-MiniLM-L6-v2")
    cross_encoder = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")

    feature_svc = FeatureService(redis=redis_client)
    scorer = AtoScorer(model_path)
    retriever = PolicyRetriever(
        pg_conn=pg_conn,
        es=es_client,
        model=embed_model,
        cross_encoder=cross_encoder,
    )
    prompt_registry = YamlPromptRegistry()
    llm_client = OpenAILLMClient()
    gate = PolicyGate(client=llm_client, prompt_registry=prompt_registry)

    # The store is reasoner-agnostic. The deployment composer supplies a
    # deserializer that knows how to reconstitute this reasoner's raw_event
    # JSON back into its concrete typed Observation for replay.
    def _ato_raw_event_factory(data: dict[str, Any]) -> LoginEvent:
        return LoginEvent.model_validate(data, strict=False)

    store = BundleStore(conn=pg_conn, raw_event_factory=_ato_raw_event_factory)
    store.ensure_schema()

    application.state.feature_svc = feature_svc
    application.state.scorer = scorer
    application.state.retriever = retriever
    application.state.gate = gate
    application.state.store = store
    application.state.corpus_version = ato.corpus_version

    log.info("api.startup", component="api", msg="All services initialized")

    yield

    pg_conn.close()
    redis_client.close()
    es_client.close()
    log.info("api.shutdown", component="api", msg="Connections closed")


# ---------------------------------------------------------------------------
# Application — mounts framework routes + each reasoner's router
# ---------------------------------------------------------------------------

app = FastAPI(
    title="DecisionLedger",
    description="Model-agnostic governed decision API.",
    version="0.1.0",
    lifespan=lifespan,
)

# Reasoner routers — the only sanctioned app/ → reasoner/ import seam.
app.include_router(ato_router)


# ---------------------------------------------------------------------------
# Framework-side endpoints (decision lookup + replay are reasoner-agnostic)
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check."""
    return {"status": "ok"}


@app.get(
    "/api/v1/decisions/{decision_id}",
    responses={404: {"model": ErrorResponse}},
)
def get_decision(decision_id: str, request: Request) -> dict[str, Any]:
    """Retrieve a stored DecisionBundle by ID (any reasoner).

    Args:
        decision_id: UUID of the decision to retrieve.
        request: FastAPI request (for accessing app.state services).

    Returns:
        Full DecisionBundle as JSON.
    """
    store: BundleStore = request.app.state.store
    try:
        bundle = store.load(decision_id)
    except KeyError as err:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "DECISION_NOT_FOUND",
                "message": f"No decision found for id={decision_id!r}",
            },
        ) from err
    return bundle.model_dump(mode="json")


@app.post(
    "/api/v1/decisions/{decision_id}/replay",
    response_model=ReplayResponse,
    responses={404: {"model": ErrorResponse}},
)
def replay_decision(decision_id: str, request: Request) -> ReplayResponse:
    """Replay enforcement against cached gate output (any reasoner).

    Re-executes the deterministic enforcement layer against the stored
    bundle's gate output. The LLM is never re-invoked. A mismatch
    between original and replayed actions indicates a non-determinism
    bug in the enforcement code.

    Args:
        decision_id: UUID of the decision to replay.
        request: FastAPI request (for accessing app.state services).

    Returns:
        ReplayResponse with original vs replayed action comparison.
    """
    store: BundleStore = request.app.state.store
    try:
        result = store.replay(decision_id)
    except KeyError as err:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "DECISION_NOT_FOUND",
                "message": f"No decision found for id={decision_id!r}",
            },
        ) from err
    return ReplayResponse(
        decision_id=result.decision_id,
        original_action=result.original_action,
        replayed_action=result.replayed_action,
        actions_match=result.actions_match,
    )
