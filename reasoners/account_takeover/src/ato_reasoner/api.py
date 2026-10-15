"""FastAPI route for the ATO Reasoner service.

Exposes ``POST /api/v1/ato/decisions`` — ATO's own decision endpoint, served
by ATO's own application (``ato_reasoner.app:create_app``), not by the
ledger (DR-31). The handler reads ATO's runtime from
``request.app.state.ato``: its services (feature windows, scorer) and the
``LedgerClient`` it submits through.

The ledger's own routes — decision lookup, replay, the subject timeline —
are served by the ledger (``app.main:create_app``), for every reasoner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from ato_reasoner.events import LoginEvent
from ato_reasoner.pipeline import run_ato_decision

if TYPE_CHECKING:
    from app.client import LedgerClient
    from ato_reasoner.plugin import AtoServices

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AtoRuntime:
    """What the ATO route needs: its services and the ledger it submits to.

    Args:
        services: Feature service, scorer, and corpus version stamp.
        client: The ledger (``LocalClient`` or ``HttpClient``).
    """

    services: AtoServices
    client: LedgerClient


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
    any; ``gates`` lists every gate of the decision's gate set;
    ``latency_ms`` is the whole decision, ATO's steps and the ledger's.
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
    policies and judges it), and returns a summary of the ledger's decision.
    The complete DecisionBundle is in the ledger.

    Args:
        request: FastAPI request. The JSON body is a ``LoginEvent``; the
            app state carries ATO's runtime.

    Returns:
        ``AtoDecisionResponse`` summary with decision_id and decision_action.

    Raises:
        LedgerError: The ledger refused the submission (the ATO app renders
            it as the ledger's problem document).
    """
    event = parse_login_event(await request.body())
    runtime: AtoRuntime = request.app.state.ato
    decision = await run_ato_decision(
        event=event, services=runtime.services, client=runtime.client
    )
    receipt = decision.receipt

    log.info(
        "ato_api.decision_complete",
        component="ato_api",
        decision_id=receipt.decision_id,
        event_id=receipt.event_id,
        decision_action=receipt.decision_action.value,
        mode=receipt.mode,
        duration_ms=decision.total_ms,
    )

    return AtoDecisionResponse(
        decision_id=receipt.decision_id,
        decision_action=receipt.decision_action.value,
        mode=receipt.mode,
        bypass_rule_id=receipt.bypass_rule_id,
        bypass_rule_holds=receipt.bypass_rule_holds,
        enforcement_rule_applied=receipt.enforcement_rule_applied,
        gates=[
            AtoGateSummary(
                gate_id=gate.gate_id,
                mandatory=gate.mandatory,
                status=gate.status,
                action=gate.action.value if gate.action else None,
            )
            for gate in receipt.gates
        ],
        latency_ms=decision.total_ms,
    )
