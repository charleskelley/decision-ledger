"""Behavioral tests for DecisionBundle (schema version 2) and GateRecord.

DecisionBundle is the convergence point — it composes every other core
contract. These tests verify that a version-2 bundle stores the reasoner's
``Submission`` verbatim and one ``GateRecord`` per gate, that each record's
fields match its status (``ran`` / ``skipped`` / ``failed``), that a record's
bypass check travels with recorded decisions only, that the bundle
round-trips through JSON (the store's path), and that it is immutable.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from core.actions import DecisionAction
from core.bundle import BUNDLE_SCHEMA_VERSION, DecisionBundle, GateRecord
from core.enforcement import EnforcementDecision
from core.gate import GateInput, GateOutput
from core.submission import evaluate_bypass
from tests._stub_reasoner.registry import LOW_SCORE_RULE

ALLOW, HOLD = DecisionAction.ALLOW, DecisionAction.HOLD


def _enforcement(action: DecisionAction = ALLOW, rule: str | None = None):
    return EnforcementDecision(
        decision_action=action, enforcement_rule_applied=rule, override_log=["x"]
    )


def _ran(gate_output: GateOutput, *, mandatory: bool = False) -> GateRecord:
    return GateRecord(
        gate_id=gate_output.gate_id,
        mandatory=mandatory,
        status="ran",
        input=GateInput(gate_id=gate_output.gate_id),
        output=gate_output,
        enforcement=_enforcement(),
        duration_ms=1.5,
    )


def _bundle(submission, gates=(), **overrides) -> DecisionBundle:
    check = (
        evaluate_bypass(LOW_SCORE_RULE, submission.reasoner_context)
        if submission.mode == "record"
        else None
    )
    base = {
        "decision_id": "dec-001",
        "created_at": datetime(2024, 1, 15, 10, 30, 2, tzinfo=UTC),
        "submission": submission,
        "idempotency_key": "evt-test-001",
        "ingestion_timestamp": datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
        "gates": tuple(gates),
        "decision_action": ALLOW,
        "decision_level_rule": None,
        "override_log": ("combinator: most_restrictive(proposed=ALLOW) → ALLOW",),
        "bypass_rule_holds": check.holds if check is not None else None,
        "bypass_check": check,
        "latency_breakdown": {"gate_ms": 0.0, "enforcement_ms": 0.1},
    }
    base.update(overrides)
    return DecisionBundle(**base)


# ---------------------------------------------------------------------------
# DecisionBundle
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_a_recorded_bundle_stores_the_submission_and_its_bypass_check(submission):
    bundle = _bundle(
        submission,
        gates=[
            GateRecord(gate_id="fake", mandatory=False, status="skipped", reason="r")
        ],
    )
    assert bundle.schema_version == BUNDLE_SCHEMA_VERSION == 2
    assert bundle.submission == submission
    assert bundle.bypass_rule_holds is True
    assert bundle.bypass_check is not None
    assert bundle.bypass_check.rule == LOW_SCORE_RULE
    assert bundle.gate("fake") is not None
    assert bundle.gate("other") is None


def test_a_gated_bundle_carries_its_gate_records_and_no_bypass_check(
    gated_submission, gate_output
):
    bundle = _bundle(gated_submission, gates=[_ran(gate_output)])
    assert bundle.bypass_check is None
    assert bundle.bypass_rule_holds is None
    record = bundle.gates[0]
    assert record.output == gate_output
    assert record.enforced_action == ALLOW


def test_a_record_must_carry_its_bypass_check(submission):
    with pytest.raises(ValidationError, match="bypass_check is set exactly"):
        _bundle(submission, bypass_check=None, bypass_rule_holds=None)


def test_a_gated_decision_carries_no_bypass_check(gated_submission, submission):
    check = evaluate_bypass(LOW_SCORE_RULE, submission.reasoner_context)
    with pytest.raises(ValidationError, match="bypass_check is set exactly"):
        _bundle(gated_submission, bypass_check=check, bypass_rule_holds=True)


def test_bypass_rule_holds_must_agree_with_the_check(submission):
    with pytest.raises(ValidationError, match=r"must equal bypass_check\.holds"):
        _bundle(submission, bypass_rule_holds=False)


def test_a_gate_appears_at_most_once(gated_submission, gate_output):
    with pytest.raises(ValidationError, match="more than once"):
        _bundle(gated_submission, gates=[_ran(gate_output), _ran(gate_output)])


def test_the_v1_layers_are_gone():
    fields = set(DecisionBundle.model_fields)
    for legacy in (
        "raw_event",
        "gate_input",
        "gate_output",
        "enforcement_rule_applied",
        "queue_position",
        "retrieval_query",
        "retrieval_results",
        "retrieval_path",
        "index_version",
    ):
        assert legacy not in fields


def test_a_bundle_round_trips_through_json(gated_submission, gate_output, submission):
    for bundle in (
        _bundle(gated_submission, gates=[_ran(gate_output)]),
        _bundle(submission),
    ):
        assert DecisionBundle.model_validate_json(bundle.model_dump_json()) == bundle


def test_a_bundle_rejects_unknown_fields(submission):
    with pytest.raises(ValidationError):
        _bundle(submission, route="FAST_PATH_ALLOW")


def test_a_bundle_is_immutable(submission):
    bundle = _bundle(submission)
    with pytest.raises(ValidationError):
        bundle.decision_action = HOLD


# ---------------------------------------------------------------------------
# GateRecord status semantics
# ---------------------------------------------------------------------------


def test_a_ran_record_needs_input_output_and_enforcement(gate_output):
    with pytest.raises(ValidationError, match="records its input, output"):
        GateRecord(gate_id="fake", mandatory=False, status="ran", output=gate_output)


def test_a_ran_record_belongs_to_its_gate(gate_output):
    with pytest.raises(ValidationError, match="belong to its gate"):
        GateRecord(
            gate_id="other",
            mandatory=False,
            status="ran",
            input=GateInput(gate_id="other"),
            output=gate_output,
            enforcement=_enforcement(),
        )


def test_a_failed_record_has_a_reason_and_fail_closed_enforcement():
    failed = GateRecord(
        gate_id="fake",
        mandatory=True,
        status="failed",
        reason="raised RuntimeError: boom",
        input=GateInput(gate_id="fake"),
        enforcement=_enforcement(HOLD, "tier1_schema_failure"),
        duration_ms=0.4,
    )
    assert failed.enforced_action == HOLD
    with pytest.raises(ValidationError, match="a failed gate"):
        GateRecord(gate_id="fake", mandatory=True, status="failed", reason="x")
    with pytest.raises(ValidationError, match="a failed gate"):
        GateRecord(
            gate_id="fake",
            mandatory=True,
            status="failed",
            enforcement=_enforcement(HOLD),
        )


def test_a_failed_record_may_have_no_input_when_the_gate_was_never_invoked():
    never = GateRecord(
        gate_id="fake",
        mandatory=False,
        status="failed",
        reason="no gate 'fake' is composed in this deployment",
        enforcement=_enforcement(HOLD, "tier1_schema_failure"),
    )
    assert never.input is None


def test_a_skipped_record_carries_only_its_reason(gate_output):
    skipped = GateRecord(gate_id="fake", mandatory=False, status="skipped", reason="r")
    assert skipped.enforced_action is None
    with pytest.raises(ValidationError, match="only why it was skipped"):
        GateRecord(gate_id="fake", mandatory=False, status="skipped")
    with pytest.raises(ValidationError, match="only why it was skipped"):
        GateRecord(
            gate_id="fake",
            mandatory=False,
            status="skipped",
            reason="r",
            output=gate_output,
        )


def test_an_unknown_status_is_rejected():
    with pytest.raises(ValidationError):
        GateRecord(
            gate_id="fake",
            mandatory=False,
            status="pending",  # type: ignore[arg-type]
            reason="r",
        )
