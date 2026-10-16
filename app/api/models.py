"""Wire models of the ledger API — what the routes return and the clients parse.

The request body of a decision is the ``core.submission.Submission`` itself
(and one per line for a batch); a lookup returns the stored
``core.bundle.DecisionBundle``. Everything else the API exchanges is defined
here: the receipt a write returns, replay results, batch results, a subject's
timeline, resolutions, and the registrations and gates resources.
``HttpClient`` parses responses into these models and ``LocalClient`` builds
the same ones, so a caller sees one set of types whichever client it uses.

Response models ignore unknown fields, so a client keeps working when the
ledger adds one.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.enforcement.resolver import credited_rule
from core.actions import DecisionAction
from core.submission import SubmissionMode, storage_problem

if TYPE_CHECKING:
    from core.bundle import DecisionBundle


class _Response(BaseModel):
    """Base of every response model: frozen, tolerant of new fields."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class ProblemDocument(BaseModel):
    """An error as the API serves it (RFC 9457 ``application/problem+json``).

    Attributes:
        type: A URN naming the problem type (derived from ``code``).
        title: Short summary of the problem type.
        status: The HTTP status.
        detail: What went wrong in this occurrence.
        code: The stable machine-readable code clients switch on.
        instance: The request path, when known.
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    type: str
    title: str
    status: int
    detail: str
    code: str
    instance: str | None = None


class GateSummary(_Response):
    """How one gate of a decision's gate set ended.

    Attributes:
        gate_id: The gate.
        mandatory: Whether it was mandatory for this decision.
        status: ``ran``, ``skipped``, or ``failed``.
        action: Its gate-level enforced action (``None`` when skipped).
        reason: Why it was skipped or failed (``None`` when it ran).
    """

    gate_id: str
    mandatory: bool
    status: str
    action: DecisionAction | None
    reason: str | None = None


class DecisionReceipt(_Response):
    """What a write returns: the decision's outcome, not its full record.

    The full bundle is at ``GET /api/v1/decisions/{decision_id}`` (DR-16).

    Attributes:
        decision_id: The new decision.
        reasoner_id: The submitting reasoner.
        event_id: The submission's event id.
        entity_id: The decision's subject, as the ledger resolved it (DR-32):
            the timeline it is filed under.
        entity_type: That subject's type.
        mode: ``record`` or ``gate``.
        decision_action: The action the ledger decided.
        decision_level_rule: The decision-level rule that fired, if any.
        enforcement_rule_applied: The one rule credited with the action, if
            any (a decision-level rule, or a gate's own tier).
        bypass_rule_id: The bypass rule a record cited (``None`` gated).
        bypass_rule_holds: Whether the cited rule held (``None`` gated).
        registration_version: The registration version the decision was
            made under.
        gates: Every gate of the decision's gate set.
        latency_breakdown: The ledger's per-step durations in milliseconds.
        created_at: When the bundle was written.
    """

    decision_id: str
    reasoner_id: str
    event_id: str
    entity_id: str
    entity_type: str | None = None
    mode: SubmissionMode
    decision_action: DecisionAction
    decision_level_rule: str | None
    enforcement_rule_applied: str | None
    bypass_rule_id: str | None
    bypass_rule_holds: bool | None
    registration_version: str | None
    gates: list[GateSummary]
    latency_breakdown: dict[str, float]
    created_at: datetime

    @classmethod
    def from_bundle(cls, bundle: DecisionBundle) -> DecisionReceipt:
        """Summarize a written bundle."""
        submission = bundle.submission
        return cls(
            decision_id=bundle.decision_id,
            reasoner_id=submission.reasoner_id,
            event_id=submission.event_id,
            entity_id=str(bundle.entity_id),
            entity_type=bundle.entity_type,
            mode=submission.mode,
            decision_action=bundle.decision_action,
            decision_level_rule=bundle.decision_level_rule,
            enforcement_rule_applied=credited_rule(bundle),
            bypass_rule_id=submission.bypass.rule_id if submission.bypass else None,
            bypass_rule_holds=bundle.bypass_rule_holds,
            registration_version=bundle.registration_version,
            gates=[
                GateSummary(
                    gate_id=record.gate_id,
                    mandatory=record.mandatory,
                    status=record.status,
                    action=record.enforced_action,
                    reason=record.reason,
                )
                for record in bundle.gates
            ],
            latency_breakdown=dict(bundle.latency_breakdown),
            created_at=bundle.created_at,
        )


class GateReplayResult(_Response):
    """One gate's original and replayed enforced action."""

    gate_id: str
    status: str
    original_action: str | None
    replayed_action: str | None


class ReplayResponse(_Response):
    """Result of replaying enforcement against a bundle's cached states.

    ``matched`` is the replay verdict: the decision action, every gate's
    enforced action, and (for a record) the bypass rule checks all
    reproduced. ``registration_version`` is the registration version replay
    enforced against.
    """

    decision_id: str
    original_action: str
    replayed_action: str
    actions_match: bool
    original_bypass_rule_holds: bool | None
    bypass_rule_holds: bool | None
    gates: list[GateReplayResult]
    registration_version: str | None = None
    matched: bool


class BatchLineResult(_Response):
    """The outcome of one line of a batch.

    Attributes:
        line: The 1-based line number in the request body.
        status: ``recorded`` or ``rejected``.
        decision_id: The decision written (``recorded`` only).
        event_id: The line's event id, when it parsed.
        decision_action: The decision's action (``recorded`` only).
        bypass_rule_holds: Whether the cited rule held (``recorded`` only).
        code: Why the line was rejected (a problem code; ``rejected`` only).
        detail: The rejection, in words (``rejected`` only).
    """

    line: int
    status: Literal["recorded", "rejected"]
    decision_id: str | None = None
    event_id: str | None = None
    decision_action: DecisionAction | None = None
    bypass_rule_holds: bool | None = None
    code: str | None = None
    detail: str | None = None


class BatchResult(_Response):
    """What ``POST /api/v1/decisions:batch`` returns: one result per line."""

    recorded: int
    rejected: int
    results: list[BatchLineResult]


class EntityDecision(_Response):
    """One decision in a subject's timeline.

    ``mode``, ``event_id``, and ``entity_type`` are ``None`` for a decision
    stored before schema version 2.

    Attributes:
        decision_id: The decision.
        reasoner_id: The reasoner that made it.
        created_at: When its bundle was written.
        decision_action: The action it produced.
        schema_version: Its bundle's schema version.
        mode: ``record`` or ``gate``.
        event_id: Its submission's event id.
        entity_type: Its subject's type.
        entity_id: Its subject: the timeline's subject, an id merged with it,
            or (with ``include_related``) a related subject (DR-32).
        relationship_type: ``None`` when ``entity_id`` is the timeline's
            subject or merged with it; the requested relationship type when
            it is a related subject.
    """

    decision_id: str
    reasoner_id: str
    created_at: datetime
    decision_action: str
    schema_version: int
    mode: str | None
    event_id: str | None
    entity_type: str | None
    entity_id: str | None = None
    relationship_type: str | None = None


class EntityDecisions(_Response):
    """A subject's decisions, newest first.

    Attributes:
        entity_id: The subject asked about.
        include_related: The relationship type whose subjects' decisions
            were included, if any.
        decisions: The decisions about the subject, every id merged with it,
            and the included related subjects.
    """

    entity_id: str
    include_related: str | None = None
    decisions: list[EntityDecision]


class ResolutionRequest(BaseModel):
    """A human resolution of a held (or challenged) decision.

    Attributes:
        resolver_id: Who resolved it (an analyst id), recorded verbatim.
        resolution_action: ``ALLOW`` or ``BLOCK`` — a resolution is terminal.
        note: The resolver's rationale.
        reviewer_role: Optional role (``"analyst"``, ``"senior-reviewer"``).
        reference_ticket_id: Optional external ticket reference.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    resolver_id: str = Field(min_length=1)
    resolution_action: DecisionAction
    note: str = Field(min_length=1)
    reviewer_role: str | None = None
    reference_ticket_id: str | None = None

    @field_validator("resolution_action")
    @classmethod
    def _terminal(cls, action: DecisionAction) -> DecisionAction:
        if action not in (DecisionAction.ALLOW, DecisionAction.BLOCK):
            msg = "a resolution is terminal: ALLOW or BLOCK"
            raise ValueError(msg)
        return action

    @model_validator(mode="after")
    def _storable(self) -> ResolutionRequest:
        """Every value is one the journal can store (no NUL, valid Unicode)."""
        problem = storage_problem(self.model_dump(mode="json"))
        if problem is not None:
            msg = f"the resolution holds {problem}, which the ledger cannot store"
            raise ValueError(msg)
        return self


class ResolutionReceipt(_Response):
    """What ``POST /api/v1/decisions/{id}/resolutions`` returns.

    Attributes:
        decision_id: The resolved decision.
        attempt_id: The resolution attempt written.
        sequence: Its position in the decision's attempt chain.
        resolver_kind: ``HUMAN``.
        resolver_id: Who resolved it.
        status: ``COMPLETED``.
        resolution_action: The resolver's action.
        realized_action: The action now realized on the subject.
        completed_at: When the attempt completed.
    """

    decision_id: str
    attempt_id: str
    sequence: int
    resolver_kind: str
    resolver_id: str
    status: str
    resolution_action: DecisionAction
    realized_action: DecisionAction | None
    completed_at: datetime


class RegisteredReasonerView(_Response):
    """One registration the deployment serves.

    Attributes:
        reasoner_id: The reasoner.
        reasoner_name: Its display name.
        registration_version: The registration's content address.
        source: ``plugin:<module>``, ``file:<path>``, or ``inline``.
        registration: The registration in its canonical JSON form.
    """

    reasoner_id: str
    reasoner_name: str
    registration_version: str
    source: str
    registration: dict[str, Any]


class ReasonerList(_Response):
    """The registrations resource: every reasoner the deployment serves."""

    reasoners: list[RegisteredReasonerView]


class RegistrationSnapshotView(_Response):
    """One stored registration version (any version a decision may cite)."""

    reasoner_id: str
    registration_version: str
    first_registered_at: datetime
    registration: dict[str, Any]


class GateView(_Response):
    """One gate the deployment composes.

    Attributes:
        gate_id: The gate.
        kind: ``llm``, ``lookup``, or ``rules``.
        deterministic: Whether its output is a pure function of its input.
        mandatory: Whether the deployment runs it on every decision.
        config_schema: The JSON schema of the ``gate_config`` it accepts,
            or ``None`` when it validates on consumption.
    """

    gate_id: str
    kind: str
    deterministic: bool
    mandatory: bool
    config_schema: dict[str, Any] | None


class GateList(_Response):
    """The gates resource: every gate the deployment composes."""

    gates: list[GateView]
