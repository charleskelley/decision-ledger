"""Behavioral tests for GateRequest — what the framework hands a gate.

GateRequest is the gate's whole view of a decision: the decision id, the
reasoner's ``Submission``, the GateContext addressed to the gate, and a
positive budget.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.gate_request import GateRequest
from core.observation import GateContext


def test_gate_request_carries_the_submission_and_its_gate_context(
    gated_submission, gate_context
):
    request = GateRequest(
        decision_id="dec-001",
        submission=gated_submission,
        gate_context=gate_context,
        budget_ms=30_000,
    )
    assert request.submission == gated_submission
    assert request.submission.reasoner_context.reasoner_id == "stub-reasoner"
    assert request.gate_context.gate_id == "fake"


def test_gate_request_rejects_an_object_that_is_not_a_submission(gate_context):
    with pytest.raises(ValidationError):
        GateRequest(
            decision_id="dec-001",
            submission={"event_id": "e"},  # type: ignore[arg-type]
            gate_context=gate_context,
            budget_ms=1_000,
        )


def test_the_old_observation_field_is_gone(gated_submission, gate_context):
    # A request that is complete but for the old field still fails, on that
    # field: unknown fields are refused, not ignored.
    assert "observation" not in GateRequest.model_fields
    with pytest.raises(ValidationError) as exc_info:
        GateRequest(
            decision_id="dec-001",
            submission=gated_submission,
            observation=gated_submission,  # type: ignore[call-arg]
            gate_context=gate_context,
            budget_ms=1_000,
        )
    assert [(e["type"], e["loc"]) for e in exc_info.value.errors()] == [
        ("extra_forbidden", ("observation",))
    ]


@pytest.mark.parametrize("budget_ms", [0, -1])
def test_gate_request_requires_a_positive_budget(gated_submission, budget_ms):
    with pytest.raises(ValidationError):
        GateRequest(
            decision_id="dec-001",
            submission=gated_submission,
            gate_context=GateContext(gate_id="fake"),
            budget_ms=budget_ms,
        )


def test_gate_request_is_frozen(gated_submission, gate_context):
    request = GateRequest(
        decision_id="dec-001",
        submission=gated_submission,
        gate_context=gate_context,
        budget_ms=1_000,
    )
    with pytest.raises(ValidationError):
        request.decision_id = "other"
