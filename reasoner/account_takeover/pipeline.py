"""ATO domain pipeline — wraps framework execute_decision with domain steps.

``run_ato_decision()`` runs the ATO-specific portion of the pipeline against
a single ``LoginEvent``: feature computation, scoring, query rendering, and
observation assembly. It then delegates to
``app.decide.execute_decision()`` (framework half) which handles retrieval,
the policy gate, enforcement, bundle assembly, and persistence.

This split keeps the framework domain-agnostic — ``app/decide.py`` does not
import anything from ``reasoner.*``.

Async (DR-23): the single ``await`` point is the policy gate call inside
``execute_decision``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from app.decide import execute_decision
from app.monitoring import duration_ms
from reasoner.account_takeover.assembler import build_observation
from reasoner.account_takeover.retrieval_query import build_ato_query

if TYPE_CHECKING:
    from app.audit import BundleStore
    from app.gate.policy import PolicyGate
    from app.retrieval.retriever import PolicyRetriever
    from core.bundle import DecisionBundle
    from reasoner.account_takeover.events import LoginEvent
    from reasoner.account_takeover.feature_service import FeatureService
    from reasoner.account_takeover.scorer import AtoScorer


async def run_ato_decision(
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
    """Run the full ATO decision pipeline for a single ``LoginEvent``.

    Domain steps (features → score → assemble) execute first, then the
    framework's ``execute_decision()`` finishes the flow (retrieval → gate
    → enforce → bundle → persist).

    Args:
        event: Validated login event to evaluate.
        feature_svc: Sliding-window feature computer (Redis-backed).
        scorer: ATO XGBoost fast scorer.
        retriever: Hybrid policy retriever (framework-owned).
        gate: LLM policy gate (framework-owned).
        store: Bundle persistence store (framework-owned).
        corpus_version: Policy corpus version stamp for the bundle.
        decision_id: Optional explicit UUID; auto-generated when omitted.
        idempotency_key: Optional explicit key; defaults to
            ``event.event_id``.

    Returns:
        The fully-assembled ``DecisionBundle`` after persistence.

    Side effects:
        - Updates Redis sliding-window features for the event's entity.
        - Reads from pgvector + Elasticsearch when routed to the gate.
        - Calls the LLM API when routed to the gate.
        - Writes the bundle to Postgres.
    """
    upstream_latency: dict[str, float] = {}

    # --- Features ---
    t0 = time.perf_counter()
    features = feature_svc.compute(event)
    upstream_latency["features_ms"] = duration_ms(t0)

    # --- Scorer ---
    t0 = time.perf_counter()
    scorer_output = scorer.score(features)
    upstream_latency["scorer_ms"] = duration_ms(t0)

    # --- Query construction (domain-owned; framework retriever takes a string) ---
    query = build_ato_query(event, scorer_output)

    # --- Assembly ---
    observation = build_observation(event, features, scorer_output)

    # --- Hand off to framework ---
    # The cast is for pyright: LoginEvent satisfies the Observation protocol
    # structurally, but its computed_field entity_id and Literal entity_type
    # confuse Pyright's invariance checks.
    return await execute_decision(
        observation=observation,  # type: ignore[arg-type]
        query=query,
        retriever=retriever,
        gate=gate,
        store=store,
        corpus_version=corpus_version,
        ingestion_timestamp=event.timestamp,
        upstream_latency=upstream_latency,
        decision_id=decision_id,
        idempotency_key=idempotency_key,
    )
