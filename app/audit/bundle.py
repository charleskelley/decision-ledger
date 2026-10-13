"""Pure DecisionBundle assembly — no I/O, no infrastructure dependencies.

build_bundle() takes all pipeline outputs and assembles the complete
DecisionBundle record. It is the single place where all pipeline layer outputs
are joined into the canonical audit record.

No database writes here — assembly is pure so it can be unit-tested without
any infrastructure running.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from core.bundle import DecisionBundle

if TYPE_CHECKING:
    from core.enforcement import EnforcementDecision
    from core.gate import GateInput, GateOutput
    from core.observation import Observation


def build_bundle(
    *,
    decision_id: str,
    obs: Observation,
    idempotency_key: str,
    ingestion_timestamp: datetime,
    queue_position: int | None = None,
    gate_input: GateInput | None = None,
    gate_output: GateOutput | None = None,
    enforcement_decision: EnforcementDecision,
    latency_breakdown: dict[str, float] | None = None,
) -> DecisionBundle:
    """Assemble a complete DecisionBundle from all pipeline layer outputs.

    This is a pure function — no I/O, no infrastructure. All inputs must be
    fully resolved before calling. The result is the canonical audit record
    for the decision.

    On the fast path no gate ran: ``gate_input`` and ``gate_output`` are both
    ``None``. On the gate path both are populated: ``gate_input`` is the
    framework's record of the invocation and ``gate_output`` the universal
    ``GateOutput`` the gate returned, whose opaque ``evidence_snapshot`` and
    ``extra`` carry everything gate-specific (DR-28). The framework does not
    interpret them; each gate documents what it writes.

    Args:
        decision_id: UUID string for this decision record.
        obs: The validated domain Observation (carries all domain provenance).
        idempotency_key: SHA-256 dedup key computed at ingestion.
        ingestion_timestamp: UTC timestamp when the event was pulled from queue.
        queue_position: Stream sequence number from the event queue, if available.
        gate_input: The framework's record of the gate invocation, or
            ``None`` when no gate ran.
        gate_output: The gate's ``GateOutput``, or ``None`` when no gate ran.
        enforcement_decision: Resolved decision action with override log from
            the enforcement layer.
        latency_breakdown: Per-component duration in milliseconds. Defaults to
            empty dict when not provided.

    Returns:
        Fully assembled ``DecisionBundle``.

    Raises:
        ValueError: Exactly one of ``gate_input`` and ``gate_output`` is set;
            a gate invocation records both or neither.
    """
    if (gate_input is None) != (gate_output is None):
        msg = (
            "gate_input and gate_output must both be set (a gate ran) or both be "
            "None (no gate ran)"
        )
        raise ValueError(msg)
    latency: dict[str, float] = (
        latency_breakdown if latency_breakdown is not None else {}
    )

    return DecisionBundle(
        decision_id=decision_id,
        created_at=datetime.now(UTC),
        raw_event=obs,
        idempotency_key=idempotency_key,
        ingestion_timestamp=ingestion_timestamp,
        queue_position=queue_position,
        gate_input=gate_input,
        gate_output=gate_output,
        decision_action=enforcement_decision.decision_action,
        enforcement_rule_applied=enforcement_decision.enforcement_rule_applied,
        override_log=enforcement_decision.override_log,
        latency_breakdown=latency,
    )
