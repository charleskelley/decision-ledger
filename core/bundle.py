"""DecisionBundle — complete replayable audit record for every decision."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from core.actions import DecisionAction
from core.gate import GateInput, GateOutput
from core.observation import Observation


class DecisionBundle(BaseModel):
    """Complete, replayable audit record for every decision.

    Stored in PostgreSQL, indexed by ``decision_id``. Every field is logged at
    decision time — nothing is computed on read, and the row is never updated
    once written. Append-only ledger semantics.

    Domain-specific artifacts (feature snapshot, reasoner output, model
    versions for the domain reasoner) are carried in ``raw_event``: the
    Observation stores a ``fast_path_rationale`` on the fast path and a
    fully rendered ``GateContext`` on all paths. Gate artifacts are carried
    in ``gate_output``: the universal base enforcement reads (verdict,
    ``deterministic``, ``evidence``) plus the gate's opaque extension
    (``evidence_snapshot``, ``extra``) stored verbatim (DR-28). A retrieving
    gate records its retrieval inside its own ``evidence_snapshot``; the
    bundle has no retrieval layer of its own.

    Replay guarantee: given a ``DecisionBundle``, re-executing enforcement
    against ``raw_event`` and ``gate_output`` produces the same
    ``decision_action``. Replay does **not** re-invoke the gate and reads no
    corpus. This holds regardless of whether the gate was an LLM, a rule
    engine, or any other implementation.

    Action vocabulary:
        * ``decision_action`` — the action this bundle produced at decision
          time. Immutable. Lives on the bundle.
        * ``resolution_action`` — for non-terminal decisions (CHALLENGE, HOLD),
          the action a resolver later produced. Lives on
          ``core.resolution.ResolutionAttempt`` rows; never on the bundle.
        * ``realized_action`` — the action ultimately taken on the entity.
          Computed from the bundle plus the resolution attempt log; never
          stored. For terminal ``decision_action`` values it equals
          ``decision_action``; otherwise it is the first terminal
          ``resolution_action`` in the attempt chain (or ``None`` if pending).

    Gate-layer field semantics by case:
        * Fast path (gate not invoked): ``gate_input is None and
          gate_output is None``.
        * Gate ran, verdict valid: both populated; ``gate_output.verdict``
          is the typed verdict enforcement consumes.
        * Gate ran, no valid verdict: both populated; ``gate_output.verdict
          is None`` and the gate's ``extra`` may carry forensic evidence.
          Enforcement routes to HOLD via the schema-failure tier.

    Attributes:
        decision_id: UUID of this decision record.
        created_at: When the bundle was constructed (UTC).
        raw_event: The validated domain Observation. Carries ``gate_context``
            (always) and ``fast_path_rationale`` (fast path only), which
            together contain all domain-specific provenance for replay and
            audit.
        idempotency_key: SHA-256 deduplication key computed at ingestion.
        ingestion_timestamp: When the event was acknowledged from the queue.
        queue_position: Stream sequence number, if available.
        gate_input: The framework's record that it invoked a gate (the
            gate's identity). ``None`` on the fast path.
        gate_output: The universal ``GateOutput`` the gate returned, with
            its opaque extension. ``None`` on the fast path. Populated even
            when the gate produced no valid verdict — in that case
            ``gate_output.verdict is None``.
        decision_action: The action this decision produced. Immutable. For
            terminal actions (ALLOW, BLOCK) this is the realized action; for
            non-terminal actions (CHALLENGE, HOLD) the realized action is
            computed from the resolution attempt log.
        enforcement_rule_applied: Override rule that fired, if any.
        override_log: Ordered rule evaluation log from enforcement.
        latency_breakdown: Per-component duration in milliseconds, keyed by
            component name (e.g., ``"features_ms"``, ``"gate_ms"``,
            ``"enforcement_ms"``).
    """

    model_config = ConfigDict(strict=True, frozen=True, arbitrary_types_allowed=True)

    # Identity
    decision_id: str
    created_at: datetime

    # Input layer — domain provenance is in raw_event.gate_context
    # and raw_event.fast_path_rationale (fast path only)
    raw_event: Observation
    idempotency_key: str
    ingestion_timestamp: datetime
    queue_position: int | None

    # Gate layer — universal contracts, both None on the fast path
    gate_input: GateInput | None
    gate_output: GateOutput | None

    # Decision layer
    decision_action: DecisionAction
    enforcement_rule_applied: str | None
    override_log: list[str]

    # Telemetry
    latency_breakdown: dict[str, float]
