"""FastAPI entry point — wires all pipeline services into HTTP endpoints.

Constructs infrastructure clients and domain services at startup via the
lifespan context manager. All connections are shared across requests and
closed on shutdown.

Run locally::

    uv run uvicorn app.main:app --reload
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
import redis
import structlog
from elasticsearch import Elasticsearch
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from pydantic_settings import BaseSettings
from sentence_transformers import CrossEncoder, SentenceTransformer

from app.audit import BundleStore, build_bundle
from app.enforcement.resolver import resolve
from app.features import FeatureService
from app.gate.policy import PolicyGate, YamlPromptRegistry
from app.monitoring import configure_logging, duration_ms
from app.retrieval.retriever import PolicyRetriever
from app.scorer import AtoScorer
from core.routes import GateRoute
from reasoner.account_takeover.assembler import build_observation
from reasoner.account_takeover.events import LoginEvent  # noqa: TC001

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """Environment-driven configuration with docker-compose defaults."""

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "account_takeover"
    postgres_password: str = "account_takeover"  # noqa: S105
    postgres_db: str = "account_takeover"

    redis_host: str = "localhost"
    redis_port: int = 6379

    elasticsearch_url: str = "http://localhost:9200"

    openai_api_key: str = ""

    scorer_model_path: str = "app/scorer/models/ato-v1.ubj"

    corpus_version: str = "unknown"

    log_json: bool = True
    log_level: str = "INFO"

    @property
    def postgres_dsn(self) -> str:
        """Build a PostgreSQL connection string from components."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class DecisionResponse(BaseModel):
    """Summary DTO returned by POST /api/v1/decisions."""

    decision_id: str
    decision_action: str
    enforcement_rule_applied: str | None
    route: str
    latency_ms: float


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
    """Construct all pipeline services at startup; close on shutdown."""
    settings = Settings()
    configure_logging(json_logs=settings.log_json, level=settings.log_level)

    model_path = Path(settings.scorer_model_path)
    if not model_path.exists():
        msg = (
            f"Scorer model not found at {model_path}. "
            "Run `make train` or "
            "`uv run python -m app.scorer train "
            f"--output {model_path} --samples 2000` first."
        )
        raise FileNotFoundError(msg)

    redis_client = redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        decode_responses=False,
    )
    pg_conn = psycopg.connect(settings.postgres_dsn)
    es_client = Elasticsearch(settings.elasticsearch_url)

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
    from openai import OpenAI

    gate = PolicyGate(client=OpenAI(), prompt_registry=prompt_registry)
    store = BundleStore(conn=pg_conn)
    store.ensure_schema()

    application.state.feature_svc = feature_svc
    application.state.scorer = scorer
    application.state.retriever = retriever
    application.state.gate = gate
    application.state.store = store
    application.state.corpus_version = settings.corpus_version

    log.info("api.startup", component="api", msg="All services initialized")

    yield

    pg_conn.close()
    redis_client.close()
    es_client.close()
    log.info("api.shutdown", component="api", msg="Connections closed")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="DecisionLedger — ATO Reasoner",
    description="Model-agnostic governed decision API.",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check."""
    return {"status": "ok"}


@app.post(
    "/api/v1/decisions",
    response_model=DecisionResponse,
    status_code=200,
    responses={422: {"model": ErrorResponse}},
)
def create_decision(event: LoginEvent, request: Request) -> DecisionResponse:
    """Run the full decision pipeline on a LoginEvent.

    Scores the event, retrieves relevant policies if needed, invokes
    the LLM policy gate when the route indicates, and produces a final
    action via deterministic enforcement. The complete DecisionBundle
    is persisted; this endpoint returns a summary.

    Args:
        event: Validated LoginEvent from the request body.
        request: FastAPI request (for accessing app.state services).

    Returns:
        DecisionResponse summary with decision_id and decision_action.
    """
    feature_svc: FeatureService = request.app.state.feature_svc
    scorer: AtoScorer = request.app.state.scorer
    retriever: PolicyRetriever = request.app.state.retriever
    gate: PolicyGate = request.app.state.gate
    store: BundleStore = request.app.state.store
    corpus_version: str = request.app.state.corpus_version

    latency: dict[str, float] = {}
    decision_id = str(uuid.uuid4())
    ingestion_ts = event.timestamp

    # --- Features ---
    t0 = time.perf_counter()
    features = feature_svc.compute(event)
    latency["features_ms"] = duration_ms(t0)

    # --- Scorer ---
    t0 = time.perf_counter()
    scorer_output = scorer.score(features)
    latency["scorer_ms"] = duration_ms(t0)

    # Build retrieval query BEFORE assembly (needs pre-assembly domain types)
    query = retriever.build_query(event, scorer_output)

    # --- Assembly ---
    obs = build_observation(event, features, scorer_output)

    # --- Retrieval + Gate (conditional) ---
    snippets = []
    gate_result = None
    retrieval_path = "skipped"

    if obs.route == GateRoute.ROUTE_TO_GATE:
        config = obs.gate_context.gate_config or {}
        t0 = time.perf_counter()
        snippets, retrieval_path = retriever.retrieve(
            query=query,
            k=5,
            jurisdictions=config.get("jurisdictions"),
            risk_tier=config.get("risk_tier"),
        )
        latency["retrieval_ms"] = duration_ms(t0)

        t0 = time.perf_counter()
        gate_result = gate.evaluate(
            obs,  # type: ignore[arg-type]
            snippets,
            decision_id=decision_id,
            corpus_version=corpus_version,
        )
        latency["gate_ms"] = duration_ms(t0)
        verdict = gate_result.gate_output.verdict
    else:
        latency["retrieval_ms"] = 0.0
        latency["gate_ms"] = 0.0
        verdict = None

    # --- Enforcement ---
    t0 = time.perf_counter()
    enforcement = resolve(
        obs,  # type: ignore[arg-type]
        verdict,
        snippets=snippets,
        decision_id=decision_id,
    )
    latency["enforcement_ms"] = duration_ms(t0)

    # --- Bundle assembly + persistence ---
    t0 = time.perf_counter()
    bundle = build_bundle(
        decision_id=decision_id,
        obs=obs,  # type: ignore[arg-type]
        idempotency_key=event.event_id,
        ingestion_timestamp=ingestion_ts,
        retrieval_query=query if gate_result else "",
        retrieval_results=snippets,
        retrieval_path=retrieval_path,
        gate_result=gate_result,
        enforcement_decision=enforcement,
        latency_breakdown=latency,
    )
    store.write(bundle)
    latency["bundle_ms"] = duration_ms(t0)

    total_ms = sum(latency.values())

    log.info(
        "api.decision_complete",
        component="api",
        decision_id=decision_id,
        decision_action=enforcement.decision_action.value,
        route=obs.route.value,
        duration_ms=total_ms,
    )

    return DecisionResponse(
        decision_id=decision_id,
        decision_action=enforcement.decision_action.value,
        enforcement_rule_applied=enforcement.enforcement_rule_applied,
        route=obs.route.value,
        latency_ms=total_ms,
    )


@app.get(
    "/api/v1/decisions/{decision_id}",
    responses={404: {"model": ErrorResponse}},
)
def get_decision(decision_id: str, request: Request) -> dict[str, Any]:
    """Retrieve a stored DecisionBundle by ID.

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
    """Replay enforcement against cached gate output.

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
