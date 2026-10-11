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

from ato_reasoner.events import LoginEvent
from ato_reasoner.pipeline import run_ato_decision
from ato_reasoner.registry import ATO_REGISTRATION

if TYPE_CHECKING:
    from app.plugin import FrameworkServices
    from ato_reasoner.plugin import AtoServices

log = structlog.get_logger(__name__)


class AtoDecisionResponse(BaseModel):
    """Summary DTO returned by ``POST /api/v1/ato/decisions``."""

    decision_id: str
    decision_action: str
    enforcement_rule_applied: str | None
    route: str
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
        HTTPException: 422 with Pydantic's error list when the body does
            not validate.
    """
    try:
        return LoginEvent.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


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

    Scores the event, retrieves relevant policies if needed, invokes
    the LLM policy gate when the route indicates, and produces a final
    action via deterministic enforcement. The complete DecisionBundle
    is persisted; this endpoint returns a summary.

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
