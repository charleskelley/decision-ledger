"""Entity route — one subject's decisions, newest first (DR-31, DR-32).

``GET /api/v1/entities/{entity_id}/decisions`` is the ledger read by subject:
every decision any reasoner made about one ``entity_id`` — and about every id
the identity journal merged with it — newest first, from the row's indexed
columns (so rows of every schema version are listed). With
``include_related=<type>`` the timeline also covers every subject one
relationship of that type away (a subject's group, or a group's members), as
the membership projection holds now; each decision says which subject it is
about and how that subject is linked. A reasoner key sees its own reasoner's
decisions only; the operator key sees every reasoner's, optionally narrowed
with ``reasoner_id``. Page with ``before`` and ``before_id``: the
``created_at`` and ``decision_id`` of the last decision of the previous page.
"""

from __future__ import annotations

import uuid
from datetime import datetime  # noqa: TC003 (FastAPI resolves query types)
from typing import TYPE_CHECKING

from fastapi import APIRouter, Query, Request

from app.api.errors import problem_responses
from app.api.models import EntityDecision, EntityDecisions
from app.auth import Caller  # noqa: TC001 (FastAPI resolves the dependency)
from core.exceptions import LedgerError

if TYPE_CHECKING:
    from app.auth import Principal
    from app.plugin import FrameworkServices

#: Default and largest page of a subject's timeline.
DEFAULT_LIMIT = 50
MAX_LIMIT = 500

router = APIRouter(prefix="/api/v1", tags=["entities"])


def entity_decisions(
    entity_id: str,
    *,
    principal: Principal,
    framework: FrameworkServices,
    reasoner_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
    before: datetime | None = None,
    before_id: str | None = None,
    include_related: str | None = None,
) -> EntityDecisions:
    """List a subject's decisions, newest first.

    Args:
        entity_id: The subject (a UUID).
        principal: The caller. A reasoner key is narrowed to its own
            reasoner.
        framework: The ledger's services.
        reasoner_id: Only this reasoner's decisions.
        limit: Page size (1 to ``MAX_LIMIT``).
        before: The ``created_at`` of the last decision already seen.
        before_id: The ``decision_id`` of that decision.
        include_related: A relationship type whose subjects' decisions join
            the timeline.

    Returns:
        The subject's decisions.

    Raises:
        LedgerError: 422 ``ENTITY_ID_INVALID``; 422 ``REQUEST_INVALID`` for
            a ``limit`` out of range, a malformed ``before_id``, or an empty
            ``include_related``; 403 ``REASONER_MISMATCH`` when a reasoner
            key asks for another reasoner's decisions.
    """
    if not 1 <= limit <= MAX_LIMIT:
        raise LedgerError(
            422,
            "REQUEST_INVALID",
            f"limit must be between 1 and {MAX_LIMIT}",
            extra={"errors": [{"loc": ["query", "limit"], "msg": "out of range"}]},
        )
    if include_related is not None and not include_related.strip():
        raise LedgerError(
            422,
            "REQUEST_INVALID",
            "include_related names a relationship type",
            extra={"errors": [{"loc": ["query", "include_related"], "msg": "empty"}]},
        )
    if before_id is not None and before is None:
        raise LedgerError(
            422,
            "REQUEST_INVALID",
            "before_id pages from a decision: send its created_at as before too",
        )
    if principal.kind == "reasoner":
        if reasoner_id is not None and reasoner_id != principal.reasoner_id:
            raise LedgerError(
                403,
                "REASONER_MISMATCH",
                f"this key belongs to reasoner {principal.reasoner_id!r}; it "
                f"cannot read {reasoner_id!r}'s decisions",
            )
        reasoner_id = principal.reasoner_id
    if before_id is not None:
        try:
            uuid.UUID(before_id)
        except ValueError as err:
            raise LedgerError(
                422, "REQUEST_INVALID", f"before_id {before_id!r} is not a UUID"
            ) from err
    try:
        rows = framework.store.entity_decisions(
            entity_id,
            reasoner_id=reasoner_id,
            limit=limit,
            before=before,
            before_id=before_id,
            include_related=include_related,
        )
    except KeyError as err:
        raise LedgerError(
            422, "ENTITY_ID_INVALID", f"entity_id {entity_id!r} is not a UUID"
        ) from err
    return EntityDecisions(
        entity_id=entity_id,
        include_related=include_related,
        decisions=[
            EntityDecision(
                decision_id=row.decision_id,
                reasoner_id=row.reasoner_id,
                created_at=row.created_at,
                decision_action=row.decision_action,
                schema_version=row.schema_version,
                mode=row.mode,
                event_id=row.event_id,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                relationship_type=row.relationship_type,
            )
            for row in rows
        ],
    )


@router.get(
    "/entities/{entity_id}/decisions",
    response_model=EntityDecisions,
    responses=problem_responses(401, 403, 422),
)
def get_entity_decisions(
    entity_id: str,
    request: Request,
    principal: Caller,
    reasoner_id: str | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    before: datetime | None = None,
    before_id: str | None = None,
    include_related: str | None = Query(None, min_length=1),
) -> EntityDecisions:
    """A subject's decisions, newest first (page with ``before``, ``before_id``).

    Covers the ids merged with the subject; ``include_related=<type>`` adds
    the subjects one relationship of that type away.
    """
    return entity_decisions(
        entity_id,
        principal=principal,
        framework=request.app.state.framework,
        reasoner_id=reasoner_id,
        limit=limit,
        before=before,
        before_id=before_id,
        include_related=include_related,
    )
