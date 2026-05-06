"""FastAPI route for the ATO Reasoner.

Exposes ``POST /api/v1/ato/decisions`` — the ATO-specific decision
endpoint. The router pulls services (feature_svc, scorer, retriever,
gate, store, corpus_version) lazily from ``request.app.state``; the
deployment composer (``app/main.py``) is responsible for populating
those during the lifespan startup.

This module is the ATO-side half of the FastAPI surface. Generic
framework-side routes (decision lookup by ID, replay) live in
``app/main.py`` since they don't depend on ATO domain types.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel

from reasoner.account_takeover.events import (
    LoginEvent,  # noqa: TC001 (FastAPI body type)
)
from reasoner.account_takeover.pipeline import run_ato_decision

log = structlog.get_logger(__name__)


class AtoDecisionResponse(BaseModel):
    """Summary DTO returned by ``POST /api/v1/ato/decisions``."""

    decision_id: str
    decision_action: str
    enforcement_rule_applied: str | None
    route: str
    latency_ms: float


router = APIRouter(prefix="/api/v1/ato", tags=["ato"])


@router.post(
    "/decisions",
    response_model=AtoDecisionResponse,
    status_code=200,
)
async def create_ato_decision(
    event: LoginEvent, request: Request
) -> AtoDecisionResponse:
    """Run the full ATO decision pipeline on a ``LoginEvent``.

    Scores the event, retrieves relevant policies if needed, invokes
    the LLM policy gate when the route indicates, and produces a final
    action via deterministic enforcement. The complete DecisionBundle
    is persisted; this endpoint returns a summary.

    Args:
        event: Validated LoginEvent from the request body.
        request: FastAPI request (for accessing app.state services).

    Returns:
        ``AtoDecisionResponse`` summary with decision_id and decision_action.
    """
    bundle = await run_ato_decision(
        event=event,
        feature_svc=request.app.state.feature_svc,
        scorer=request.app.state.scorer,
        retriever=request.app.state.retriever,
        gate=request.app.state.gate,
        store=request.app.state.store,
        corpus_version=request.app.state.corpus_version,
        idempotency_key=event.event_id,
    )

    total_ms = sum(bundle.latency_breakdown.values())

    log.info(
        "ato_api.decision_complete",
        component="ato_api",
        decision_id=bundle.decision_id,
        decision_action=bundle.decision_action.value,
        route=bundle.raw_event.route.value,
        duration_ms=total_ms,
    )

    return AtoDecisionResponse(
        decision_id=bundle.decision_id,
        decision_action=bundle.decision_action.value,
        enforcement_rule_applied=bundle.enforcement_rule_applied,
        route=bundle.raw_event.route.value,
        latency_ms=total_ms,
    )
