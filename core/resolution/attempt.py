"""ResolutionAttempt — universal base for resolution-attempt records.

A non-terminal ``DecisionAction`` (``CHALLENGE`` or ``HOLD``) requires one
or more ``ResolutionAttempt`` rows to reach a realized terminal action
(``ALLOW`` or ``BLOCK``). Each attempt is immutable; new states are
recorded as new attempts, never by mutating an existing row. The realized
action of a decision is computed from the bundle plus the attempt chain
— never stored.

Per DR-21, this base class carries only universal fields. Per-resolver-kind
subclasses (``HumanResolutionAttempt``, ``SlaDefaultResolutionAttempt``,
…) live in sibling modules and add typed fields specific to their kind.
The discriminator is ``resolver_kind``, narrowed to a Pydantic
``Literal[ResolverKind.<kind>]`` in each subclass.

Action vocabulary (reference; defined fully in ``core.bundle``):
    * ``decision_action`` — the action the pipeline produced. On the bundle.
    * ``resolution_action`` — the action a resolver produced. On
      ``ResolutionAttempt`` rows.
    * ``realized_action`` — derived: terminal ``decision_action`` if present,
      else the first terminal ``resolution_action`` in the attempt chain.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from core.actions import DecisionAction


class ResolverKind(StrEnum):
    """The mechanism that produced a resolution attempt.

    Attributes:
        HUMAN: Analyst review (MVP).
        SLA_DEFAULT: System-driven default action applied at SLA expiry (MVP).
        STEP_UP_AUTH: MFA / step-up authentication challenge outcome.
        AUTOMATED_OUTREACH: Email or SMS verification with the account holder.
        SECOND_OPINION: Heavyweight model or additional gate evaluation.
        EXTERNAL_TICKET: External case-management system (Jira, ServiceNow).
        SELF_SERVICE: End-user verification through the application.
        OVERRIDE: Out-of-band leadership or governance decision.
        ESCALATION: Intermediate routing to another resolver — not terminal
            on its own; produces a new attempt downstream.
    """

    HUMAN = "HUMAN"
    SLA_DEFAULT = "SLA_DEFAULT"
    STEP_UP_AUTH = "STEP_UP_AUTH"
    AUTOMATED_OUTREACH = "AUTOMATED_OUTREACH"
    SECOND_OPINION = "SECOND_OPINION"
    EXTERNAL_TICKET = "EXTERNAL_TICKET"
    SELF_SERVICE = "SELF_SERVICE"
    OVERRIDE = "OVERRIDE"
    ESCALATION = "ESCALATION"


class ResolutionStatus(StrEnum):
    """Lifecycle status of a single attempt, independent of its action outcome.

    Attributes:
        PENDING: Attempt opened, awaiting completion.
        COMPLETED: Attempt finished and produced a ``resolution_action``.
        ESCALATED: Attempt routed onward; another attempt is expected.
        EXPIRED: Attempt timed out without completion.
    """

    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    ESCALATED = "ESCALATED"
    EXPIRED = "EXPIRED"


class ResolutionAttempt(BaseModel):
    """Universal base — one attempt to resolve a non-terminal ``DecisionBundle``.

    Immutable. Multiple attempts per ``decision_id`` are ordered by
    ``sequence``. The realized action of a decision is computed by walking
    attempts in sequence order and returning the first terminal
    ``resolution_action`` (``ALLOW`` or ``BLOCK``).

    Concrete subclasses (``HumanResolutionAttempt``,
    ``SlaDefaultResolutionAttempt``, …) narrow ``resolver_kind`` to a
    Pydantic ``Literal[...]`` and add kind-specific typed fields.

    Args:
        decision_id: UUID string of the parent ``DecisionBundle``.
        attempt_id: UUID string identifying this attempt.
        sequence: Position of this attempt within the decision's attempt
            chain. Zero-based, monotonically increasing per ``decision_id``.
        started_at: When this attempt was opened (UTC).
        completed_at: When this attempt completed, or ``None`` while pending.
        resolver_kind: The mechanism that produced this attempt.
            Discriminator field — narrowed to ``Literal[...]`` in subclasses.
        resolver_id: Identifier for the actor — user_id, automation name, or
            a system label such as ``"system:sla_timer"``.
        status: Lifecycle status of this attempt.
        resolution_action: The action this attempt produced, or ``None``
            while ``status`` is ``PENDING`` / ``ESCALATED`` / ``EXPIRED`` and
            no action has been determined.
        note: Free-text note from the resolver (reviewer rationale, system
            comment, etc.).
    """

    model_config = ConfigDict(strict=True, frozen=True)

    decision_id: str
    attempt_id: str
    sequence: int = Field(ge=0)
    started_at: datetime
    completed_at: datetime | None
    resolver_kind: ResolverKind
    resolver_id: str
    status: ResolutionStatus
    resolution_action: DecisionAction | None
    note: str
