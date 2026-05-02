"""Pipeline orchestration — single source of truth for the decision flow.

``execute_pipeline()`` runs the full ATO decision sequence against a single
``LoginEvent``: feature compute → scoring → observation assembly →
(conditional) retrieval + policy gate → deterministic enforcement →
bundle assembly → persistence. Returns the persisted ``DecisionBundle``.

Used by:
- ``app/main.py:create_decision()`` — the FastAPI route handler.
- ``eval/clients/pipeline.py:PipelineDriver`` — the eval harness's
  in-process driver for consistency / robustness dimensions.

Async (DR-23). The single ``await`` point is the policy-gate call;
all other services (features, scorer, retriever, store) are sync and
run inline. The pipeline itself does not log structured per-step
events — callers log what makes sense for their context (HTTP request,
eval run, etc.).
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING

from app.audit import build_bundle
from app.enforcement.resolver import resolve
from app.monitoring import duration_ms
from core.routes import GateRoute
from reasoner.account_takeover.assembler import build_observation

if TYPE_CHECKING:
    from app.audit import BundleStore
    from app.features import FeatureService
    from app.gate.policy import PolicyGate
    from app.retrieval.retriever import PolicyRetriever
    from app.scorer import AtoScorer
    from core.bundle import DecisionBundle
    from reasoner.account_takeover.events import LoginEvent


async def execute_pipeline(
    *,
    event: LoginEvent,
    feature_svc: FeatureService,
    scorer: AtoScorer,
    retriever: PolicyRetriever,
    gate: PolicyGate,
    store: BundleStore,
    corpus_version: str,
    decision_id: str | None = None,
    idempotency_key: str | None = None,
) -> DecisionBundle:
    """Run the full decision pipeline for a single ``LoginEvent``.

    Args:
        event: Validated login event to evaluate.
        feature_svc: Sliding-window feature computer (Redis-backed).
        scorer: ATO XGBoost fast scorer.
        retriever: Hybrid policy retriever (pgvector + ES + reranker).
        gate: LLM policy gate (invoked only when scorer routes to gate).
        store: Bundle persistence store (Postgres-backed).
        corpus_version: Policy corpus version stamp for the bundle.
        decision_id: Optional explicit UUID; auto-generated when omitted.
        idempotency_key: Optional explicit key; defaults to ``event.event_id``.

    Returns:
        The fully-assembled ``DecisionBundle`` after persistence.

    Side effects:
        - Updates Redis sliding-window features for the event's entity.
        - Reads from pgvector + Elasticsearch when routed to the gate.
        - Calls the OpenAI API when routed to the gate.
        - Writes the bundle to Postgres.
    """
    decision_id = decision_id or str(uuid.uuid4())
    idempotency_key = idempotency_key or event.event_id
    ingestion_ts = event.timestamp
    latency: dict[str, float] = {}

    # --- Features ---
    t0 = time.perf_counter()
    features = feature_svc.compute(event)
    latency["features_ms"] = duration_ms(t0)

    # --- Scorer ---
    t0 = time.perf_counter()
    scorer_output = scorer.score(features)
    latency["scorer_ms"] = duration_ms(t0)

    # Build retrieval query before assembly (needs pre-assembly domain types)
    query = retriever.build_query(event, scorer_output)

    # --- Assembly ---
    obs = build_observation(event, features, scorer_output)

    # --- Retrieval + Gate (conditional on route) ---
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
        gate_result = await gate.evaluate(
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
    # Note: bundle assembly + write latency is intentionally not tracked —
    # the bundle is frozen at build_bundle() time, so any post-construction
    # measurement cannot land in the persisted record. Keeping the
    # response total and the persisted total identical is more important
    # than capturing bundle write time.
    bundle = build_bundle(
        decision_id=decision_id,
        obs=obs,  # type: ignore[arg-type]
        idempotency_key=idempotency_key,
        ingestion_timestamp=ingestion_ts,
        retrieval_query=query if gate_result else "",
        retrieval_results=snippets,
        retrieval_path=retrieval_path,
        gate_result=gate_result,
        enforcement_decision=enforcement,
        latency_breakdown=latency,
    )
    store.write(bundle)

    return bundle
