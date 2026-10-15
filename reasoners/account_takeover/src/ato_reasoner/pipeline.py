"""ATO domain pipeline — wraps the framework's ``submit`` with domain steps.

``run_ato_decision()`` runs the ATO-specific portion of the pipeline against
a single ``LoginEvent``: feature computation, scoring, and submission
assembly (which picks the mode from ATO's bypass rules and renders the policy
gate's retrieval query into its gate context). It then hands the
``Submission`` to ``app.submit.submit()`` (framework half), which runs the
decision's gate set — the advisory policy gate only when ATO asks for a gate,
plus any gate the deployment mandates — enforces, and persists the bundle.

This split keeps the framework domain-agnostic — ``app/submit.py`` does not
import anything from ``ato_reasoner``.

Async (DR-23): the awaits are the gate calls inside ``submit``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from app.monitoring import duration_ms
from app.submit import submit
from ato_reasoner.assembler import build_submission

if TYPE_CHECKING:
    from app.plugin import FrameworkServices
    from ato_reasoner.events import LoginEvent
    from ato_reasoner.plugin import AtoServices
    from core.bundle import DecisionBundle


async def run_ato_decision(
    *,
    event: LoginEvent,
    services: AtoServices,
    framework: FrameworkServices,
    decision_id: str | None = None,
    idempotency_key: str | None = None,
) -> DecisionBundle:
    """Run the full ATO decision pipeline for a single ``LoginEvent``.

    Domain steps (features → score → assemble) execute first, then the
    framework's ``submit()`` finishes the flow (gate set → enforce → bundle
    → persist).

    Args:
        event: Validated login event to evaluate.
        services: ATO's own services — feature service, scorer, and the
            corpus version stamp.
        framework: Shared framework services — the composed gates, the
            store, and the reasoner registry (which must contain
            ``ATO_REGISTRATION`` or intake validation rejects the
            submission).
        decision_id: Optional explicit UUID; auto-generated when omitted.
        idempotency_key: Optional explicit key; defaults to
            ``event.event_id``.

    Returns:
        The fully-assembled ``DecisionBundle`` after persistence.

    Side effects:
        - Updates Redis sliding-window features for the event's entity.
        - When ATO asks for a gate, the policy gate reads pgvector +
          Elasticsearch and calls the LLM API.
        - Writes the bundle to Postgres.
    """
    upstream_latency: dict[str, float] = {}

    # --- Features ---
    t0 = time.perf_counter()
    features = services.feature_svc.compute(event)
    upstream_latency["features_ms"] = duration_ms(t0)

    # --- Scorer ---
    t0 = time.perf_counter()
    scorer_output = services.scorer.score(features)
    upstream_latency["scorer_ms"] = duration_ms(t0)

    # --- Assembly (mode, gate context, and the domain-owned retrieval query) ---
    submission = build_submission(
        event, features, scorer_output, corpus_version=services.corpus_version
    )

    # --- Hand off to framework ---
    return await submit(
        submission=submission,
        gates=framework.gates,
        store=framework.store,
        registry=framework.registry,
        ingestion_timestamp=event.timestamp,
        upstream_latency=upstream_latency,
        decision_id=decision_id,
        idempotency_key=idempotency_key,
    )
