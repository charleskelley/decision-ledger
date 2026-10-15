"""FastAPI route for the ATO Reasoner.

Exposes ``POST /api/v1/ato/decisions`` — the ATO-specific decision
endpoint. The handler reads this reasoner's services from
``request.app.state.reasoners["ato-reasoner"]`` and the shared framework
services from ``request.app.state.framework``; ``app/main.py:create_app``
populates both during lifespan startup after discovering the plugin.

This module is the ATO-side half of the FastAPI surface. Generic
framework-side routes (decision lookup by ID, replay) live in
``app/main.py`` since they don't depend on ATO domain types.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from app.enforcement.resolver import credited_rule
from ato_reasoner.events import LoginEvent
from ato_reasoner.pipeline import run_ato_decision
from ato_reasoner.registry import ATO_REGISTRATION

if TYPE_CHECKING:
    from app.plugin import FrameworkServices
    from ato_reasoner.plugin import AtoServices

log = structlog.get_logger(__name__)


class AtoGateSummary(BaseModel):
    """One gate of the decision's gate set, as it ended."""

    gate_id: str
    mandatory: bool
    status: str
    action: str | None


class AtoDecisionResponse(BaseModel):
    """Summary DTO returned by ``POST /api/v1/ato/decisions``.

    ``mode`` is ``record`` when a score band's bypass rule applied (then
    ``bypass_rule_id`` names it and ``bypass_rule_holds`` says whether the
    ledger confirmed it) and ``gate`` when the login went to the gates.
    ``enforcement_rule_applied`` is the rule credited with the action, if
    any; ``gates`` lists every gate of the decision's gate set.
    """

    decision_id: str
    decision_action: str
    mode: str
    bypass_rule_id: str | None
    bypass_rule_holds: bool | None
    enforcement_rule_applied: str | None
    gates: list[AtoGateSummary]
    latency_ms: float


router = APIRouter(prefix="/api/v1/ato", tags=["ato"])


def parse_login_event(body: bytes) -> LoginEvent:
    """Validate a raw JSON request body into a ``LoginEvent``.

    ``LoginEvent`` is a strict model. Pydantic's strict mode accepts ISO-8601
    strings for datetimes and enum values for ``StrEnum`` fields only when
    validating *JSON* input; FastAPI's default body handling validates the
    already-decoded Python objects, where strict mode rejects them. Parsing
    the bytes directly keeps the model strict and the wire format ordinary.

    Args:
        body: Raw request body bytes.

    Returns:
        The validated ``LoginEvent``.

    Raises:
        HTTPException: 422 with Pydantic's error list (each error's ``type``,
            ``loc``, and ``msg``) when the body does not validate. The input
            is not echoed: it can hold what JSON cannot carry (a ``NaN``),
            and the context of a model-level error is an exception object.
    """
    try:
        return LoginEvent.model_validate_json(body)
    except ValidationError as exc:
        detail = exc.errors(
            include_url=False, include_context=False, include_input=False
        )
        raise HTTPException(status_code=422, detail=detail) from exc


@router.post(
    "/decisions",
    response_model=AtoDecisionResponse,
    status_code=200,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": LoginEvent.model_json_schema()}},
        }
    },
)
async def create_ato_decision(request: Request) -> AtoDecisionResponse:
    """Run the full ATO decision pipeline on a ``LoginEvent``.

    Scores the event, records it under a score band's bypass rule or asks
    the ledger to gate it (the LLM policy gate then retrieves relevant
    policies and judges it), and produces a final action via deterministic
    enforcement. The complete DecisionBundle is persisted; this endpoint
    returns a summary.

    Args:
        request: FastAPI request. The JSON body is a ``LoginEvent``; the
            app state carries this reasoner's services and the framework's.

    Returns:
        ``AtoDecisionResponse`` summary with decision_id and decision_action.
    """
    event = parse_login_event(await request.body())
    services: AtoServices = request.app.state.reasoners[ATO_REGISTRATION.reasoner_id]
    framework: FrameworkServices = request.app.state.framework
    bundle = await run_ato_decision(
        event=event,
        services=services,
        framework=framework,
        idempotency_key=event.event_id,
    )

    total_ms = sum(bundle.latency_breakdown.values())
    submission = bundle.submission

    log.info(
        "ato_api.decision_complete",
        component="ato_api",
        decision_id=bundle.decision_id,
        event_id=submission.event_id,
        decision_action=bundle.decision_action.value,
        mode=submission.mode,
        duration_ms=total_ms,
    )

    return AtoDecisionResponse(
        decision_id=bundle.decision_id,
        decision_action=bundle.decision_action.value,
        mode=submission.mode,
        bypass_rule_id=submission.bypass.rule_id if submission.bypass else None,
        bypass_rule_holds=bundle.bypass_rule_holds,
        enforcement_rule_applied=credited_rule(bundle),
        gates=[
            AtoGateSummary(
                gate_id=record.gate_id,
                mandatory=record.mandatory,
                status=record.status,
                action=(
                    record.enforced_action.value if record.enforced_action else None
                ),
            )
            for record in bundle.gates
        ],
        latency_ms=total_ms,
    )
