"""Behavioral tests for ``app.audit.bundle.build_bundle`` — pure v2 assembly.

``build_bundle`` joins every layer of a decision into the version-2
``DecisionBundle``: the submission verbatim, one gate record per gate, the
decision-level enforcement, and — for a record — the bypass checks. It is
pure: no I/O, a fresh UTC ``created_at``, and the layers' own validators
reject an inconsistent combination.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.audit.bundle import build_bundle
from app.enforcement.resolver import resolve_decision, resolve_gate_output
from core.actions import DecisionAction
from core.bundle import GateRecord
from core.gate import GateInput
from core.submission import evaluate_bypass, evaluate_competing_rules
from tests._stub_reasoner.registry import LOW_SCORE_RULE, STUB_REGISTRATION

_DECISION_ID = "11111111-1111-1111-1111-111111111111"


def _skipped() -> GateRecord:
    return GateRecord(gate_id="fake", mandatory=False, status="skipped", reason="r")


def _ran(gate_output) -> GateRecord:
    return GateRecord(
        gate_id="fake",
        mandatory=False,
        status="ran",
        input=GateInput(gate_id="fake"),
        output=gate_output,
        enforcement=resolve_gate_output(
            gate_output, registration=STUB_REGISTRATION, decision_id=_DECISION_ID
        ),
    )


def _record_bundle(submission, **overrides):
    check = evaluate_bypass(LOW_SCORE_RULE, submission.reasoner_context)
    competing = evaluate_competing_rules(
        STUB_REGISTRATION, LOW_SCORE_RULE, submission.reasoner_context
    )
    records = [_skipped()]
    decision = resolve_decision(
        submission=submission,
        records=records,
        registration=STUB_REGISTRATION,
        bypass_check=check,
        competing_checks=competing,
        decision_id=_DECISION_ID,
    )
    fields = {
        "decision_id": _DECISION_ID,
        "submission": submission,
        "idempotency_key": submission.event_id,
        "ingestion_timestamp": submission.timestamp,
        "gates": records,
        "decision": decision,
        "bypass_check": check,
        "other_bypass_checks": competing,
        "latency_breakdown": {"features_ms": 1.0, "gate_ms": 0.0},
    }
    fields.update(overrides)
    return build_bundle(**fields)


def test_a_record_bundle_carries_every_layer(submission):
    bundle = _record_bundle(submission)
    assert bundle.schema_version == 2
    assert bundle.decision_id == _DECISION_ID
    assert bundle.submission == submission
    assert bundle.decision_action == DecisionAction.ALLOW
    assert bundle.decision_level_rule is None
    assert bundle.bypass_rule_holds is True
    assert bundle.bypass_check is not None
    assert bundle.bypass_check.rule == LOW_SCORE_RULE
    assert [c.rule.rule_id for c in bundle.other_bypass_checks] == ["high-score"]
    assert bundle.registration_version is None
    assert bundle.gates == (_skipped(),)


def test_created_at_is_utc_and_fresh(submission):
    bundle = _record_bundle(submission)
    assert bundle.created_at.tzinfo is UTC
    assert datetime.now(UTC) - bundle.created_at < timedelta(seconds=5)


def test_the_latency_breakdown_and_decision_log_are_stored(submission):
    bundle = _record_bundle(submission)
    assert bundle.latency_breakdown == {"features_ms": 1.0, "gate_ms": 0.0}
    assert bundle.override_log[-1] == (
        "combinator: most_restrictive(proposed=ALLOW) → ALLOW"
    )


def test_a_gated_bundle_stores_each_gate_output_as_returned(
    gated_submission, gate_output
):
    record = _ran(gate_output)
    decision = resolve_decision(
        submission=gated_submission,
        records=[record],
        registration=STUB_REGISTRATION,
        bypass_check=None,
        decision_id=_DECISION_ID,
    )
    bundle = build_bundle(
        decision_id=_DECISION_ID,
        submission=gated_submission,
        idempotency_key="k",
        ingestion_timestamp=gated_submission.timestamp,
        gates=[record],
        decision=decision,
    )
    assert bundle.gates[0].output == gate_output
    assert bundle.gates[0].output.evidence_snapshot == gate_output.evidence_snapshot
    assert bundle.gates[0].output.extra == gate_output.extra
    assert bundle.bypass_check is None
    assert bundle.latency_breakdown == {}


def test_a_record_without_its_bypass_check_is_rejected(submission):
    with pytest.raises(ValidationError, match="bypass_check is set exactly"):
        _record_bundle(submission, bypass_check=None, other_bypass_checks=())


def test_competing_checks_on_a_gated_decision_are_rejected(
    gated_submission, gate_output, submission
):
    competing = evaluate_competing_rules(
        STUB_REGISTRATION, LOW_SCORE_RULE, submission.reasoner_context
    )
    record = _ran(gate_output)
    decision = resolve_decision(
        submission=gated_submission,
        records=[record],
        registration=STUB_REGISTRATION,
        bypass_check=None,
        decision_id=_DECISION_ID,
    )
    with pytest.raises(ValidationError, match="for a record only"):
        build_bundle(
            decision_id=_DECISION_ID,
            submission=gated_submission,
            idempotency_key="k",
            ingestion_timestamp=gated_submission.timestamp,
            gates=[record],
            decision=decision,
            other_bypass_checks=competing,
        )
