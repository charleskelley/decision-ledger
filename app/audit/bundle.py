"""Pure DecisionBundle assembly — no I/O, no infrastructure dependencies.

build_bundle() takes every layer of a decision and assembles the version-2
``DecisionBundle``. It is the single place where they are joined into the
canonical audit record.

No database writes here — assembly is pure so it can be unit-tested without
any infrastructure running.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from core.bundle import DecisionBundle

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.bundle import GateRecord
    from core.enforcement import EnforcementDecision
    from core.submission import BypassCheck, Submission


def build_bundle(
    *,
    decision_id: str,
    submission: Submission,
    idempotency_key: str,
    ingestion_timestamp: datetime,
    gates: Sequence[GateRecord],
    decision: EnforcementDecision,
    bypass_check: BypassCheck | None = None,
    other_bypass_checks: Sequence[BypassCheck] = (),
    latency_breakdown: dict[str, float] | None = None,
    registration_version: str | None = None,
) -> DecisionBundle:
    """Assemble a complete DecisionBundle from every layer of a decision.

    This is a pure function — no I/O, no infrastructure. All inputs must be
    fully resolved before calling. The result is the canonical audit record
    for the decision.

    Args:
        decision_id: UUID string for this decision record.
        submission: The reasoner's validated submission, stored verbatim.
        idempotency_key: Deduplication key.
        ingestion_timestamp: When the event entered the reasoner's pipeline.
        gates: One record per gate in the decision's gate set (``ran``,
            ``skipped``, or ``failed``), each with its gate-level
            enforcement.
        decision: The decision-level enforcement: the final action, the
            decision-level rule that fired, and the decision-level log.
        bypass_check: The cited rule's evaluation for a record; ``None`` for
            a gated decision.
        other_bypass_checks: For a record, the evaluations of every other
            registered bypass rule.
        latency_breakdown: Per-component duration in milliseconds. Defaults
            to an empty dict.
        registration_version: The version of the reasoner's registration
            the decision was made under (``registration_version`` of the
            registration intake used); replay reads that version's snapshot.

    Returns:
        Fully assembled version-2 ``DecisionBundle``.

    Raises:
        pydantic.ValidationError: The layers are inconsistent (a record
            without its bypass check, a gate recorded twice, a gate record
            whose fields do not match its status).
    """
    return DecisionBundle(
        decision_id=decision_id,
        created_at=datetime.now(UTC),
        submission=submission,
        idempotency_key=idempotency_key,
        ingestion_timestamp=ingestion_timestamp,
        gates=tuple(gates),
        decision_action=decision.decision_action,
        decision_level_rule=decision.enforcement_rule_applied,
        override_log=tuple(decision.override_log),
        bypass_rule_holds=bypass_check.holds if bypass_check is not None else None,
        bypass_check=bypass_check,
        other_bypass_checks=tuple(other_bypass_checks),
        registration_version=registration_version,
        latency_breakdown=dict(latency_breakdown or {}),
    )
