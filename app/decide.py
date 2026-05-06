"""Framework decision orchestrator — domain-agnostic half of the pipeline.

``execute_decision()`` runs the framework's portion of the pipeline against a
fully-assembled ``Observation``: optional retrieval + policy gate (when the
domain routed ``ROUTE_TO_GATE``), deterministic enforcement, bundle
assembly, and persistence. Returns the persisted ``DecisionBundle``.

The reasoner pipeline (``reasoner/<domain>/pipeline.py``) is responsible for
everything *before* this function: feature computation, scoring, assembly,
and rendering a retrieval query string. Once it has those, it calls
``execute_decision()`` with the assembled observation and the rendered query.

Used by:
- ``reasoner/account_takeover/pipeline.py:run_ato_decision`` — the ATO
  domain orchestrator that wraps this function.
- ``eval/clients/pipeline.py:PipelineDriver`` — the eval harness's
  in-process driver for consistency / robustness dimensions.

Async (DR-23). The single ``await`` point is the policy-gate call;
all other services (retriever, store) are sync and run inline. The function
itself does not log structured per-step events — callers log what makes
sense for their context (HTTP request, eval run, etc.).
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING

from app.audit import build_bundle
from app.enforcement.resolver import resolve
from app.monitoring import duration_ms
from core.routes import GateRoute

if TYPE_CHECKING:
    from datetime import datetime

    from app.audit import BundleStore
    from app.gate.policy import PolicyGate
    from app.retrieval.retriever import PolicyRetriever
    from core.bundle import DecisionBundle
    from core.observation import Observation


async def execute_decision(
    *,
    observation: Observation,
    query: str,
    retriever: PolicyRetriever,
    gate: PolicyGate,
    store: BundleStore,
    corpus_version: str,
    ingestion_timestamp: datetime,
    upstream_latency: dict[str, float] | None = None,
    decision_id: str | None = None,
    idempotency_key: str | None = None,
) -> DecisionBundle:
    """Run the framework half of the decision pipeline against an Observation.

    The reasoner is expected to have already run feature computation,
    scoring, and ``build_observation`` (or its equivalent) before calling
    this function. The framework consumes the assembled ``Observation``
    and the reasoner-rendered retrieval ``query`` string and produces a
    persisted ``DecisionBundle``.

    Args:
        observation: Fully-assembled Observation from the domain reasoner.
            Must satisfy the ``core.observation.Observation`` protocol —
            ``reasoner_context`` and ``gate_context`` populated.
        query: Pre-rendered retrieval query string (built by the reasoner).
            Empty string is acceptable when the route is fast-path.
        retriever: Hybrid policy retriever (pgvector + ES + reranker).
        gate: LLM policy gate (invoked only when the route is to-gate).
        store: Bundle persistence store (Postgres-backed).
        corpus_version: Policy corpus version stamp for the bundle.
        ingestion_timestamp: Wall-clock time at which the upstream raw event
            entered the pipeline. Recorded in the bundle.
        upstream_latency: Optional latency breakdown for steps the reasoner
            ran before this call (e.g., ``{"features_ms": 1.2,
            "scorer_ms": 3.1}``). Merged into the bundle's
            ``latency_breakdown``.
        decision_id: Optional explicit UUID; auto-generated when omitted.
        idempotency_key: Optional explicit key; defaults to
            ``observation.event_id``.

    Returns:
        The fully-assembled ``DecisionBundle`` after persistence.

    Side effects:
        - Reads from pgvector + Elasticsearch when routed to the gate.
        - Calls the LLM API when routed to the gate.
        - Writes the bundle to Postgres.
    """
    decision_id = decision_id or str(uuid.uuid4())
    idempotency_key = idempotency_key or observation.event_id
    latency: dict[str, float] = dict(upstream_latency or {})

    snippets = []
    gate_result = None
    retrieval_path = "skipped"

    if observation.route == GateRoute.ROUTE_TO_GATE:
        config = observation.gate_context.gate_config or {}
        reasoner_id = (
            observation.reasoner_context.reasoner_id
            if observation.reasoner_context is not None
            else None
        )
        t0 = time.perf_counter()
        snippets, retrieval_path = retriever.retrieve(
            query=query,
            k=5,
            reasoner_id=reasoner_id,
            jurisdictions=config.get("jurisdictions"),
            risk_tier=config.get("risk_tier"),
        )
        latency["retrieval_ms"] = duration_ms(t0)

        t0 = time.perf_counter()
        gate_result = await gate.evaluate(
            observation,
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
        observation,
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
        obs=observation,
        idempotency_key=idempotency_key,
        ingestion_timestamp=ingestion_timestamp,
        retrieval_query=query if gate_result else "",
        retrieval_results=snippets,
        retrieval_path=retrieval_path,
        gate_result=gate_result,
        enforcement_decision=enforcement,
        latency_breakdown=latency,
    )
    store.write(bundle)

    return bundle
