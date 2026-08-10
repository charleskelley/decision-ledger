"""HumanResolutionAttempt — analyst-review resolver subclass.

The MVP-implemented human-review variant of ``ResolutionAttempt``. Adds
typed fields for reviewer-role and external-ticket-reference metadata.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict

from core.resolution.attempt import ResolutionAttempt


class HumanResolutionAttempt(ResolutionAttempt):
    """Resolution attempt produced by an analyst review.

    Attributes:
        resolver_kind: Discriminator value ``ResolverKind.HUMAN``. Pydantic-narrowed.
        reviewer_role: Optional role of the reviewer (e.g., ``"analyst"``,
            ``"senior-reviewer"``). Used for queue routing and audit
            grouping. ``None`` when role is not tracked.
        reference_ticket_id: Optional external ticket reference (Jira,
            ServiceNow, internal review queue) the reviewer used to record
            their decision context. ``None`` when no external ticket
            backed this review.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    resolver_kind: Literal["HUMAN"] = "HUMAN"
    reviewer_role: str | None = None
    reference_ticket_id: str | None = None
