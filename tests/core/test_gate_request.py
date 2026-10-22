"""Behavioral tests for GateRequest — what the framework hands a gate.

GateRequest is the gate's whole view of a decision: the decision id, the
reasoner's ``Submission``, the GateContext addressed to the gate, a positive
budget, the decision's subject as the ledger resolved it (DR-32), and the
config of the reasoner's binding to the gate (DR-33).
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


def test_gate_request_carries_no_binding_config_by_default(
    gated_submission, gate_context
):
    request = GateRequest(
        decision_id="dec-001",
        submission=gated_submission,
        gate_context=gate_context,
        budget_ms=30_000,
    )
    assert request.binding_config == {}


def test_gate_request_carries_the_bindings_config_as_json(
    gated_submission, gate_context
):
    config = {"label": "kept", "depth": [1, 2.5, None, True], "nested": {"a": "b"}}
    request = GateRequest(
        decision_id="dec-001",
        submission=gated_submission,
        gate_context=gate_context,
        budget_ms=30_000,
        binding_config=config,
    )
    assert request.binding_config == config


def test_gate_request_refuses_a_binding_config_that_is_not_json(
    gated_submission, gate_context
):
    with pytest.raises(ValidationError):
        GateRequest(
            decision_id="dec-001",
            submission=gated_submission,
            gate_context=gate_context,
            budget_ms=30_000,
            binding_config={"bad": object()},  # type: ignore[dict-item]
        )


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


def test_gate_request_carries_the_resolved_subject(gated_submission, gate_context):
    from datetime import UTC, datetime

    from core.identity import IdentityProvenance, ResolvedIdentity

    identity = ResolvedIdentity(
        entity_id=gated_submission.entity_id,
        entity_type=gated_submission.entity_type,
        provenance=IdentityProvenance(
            resolved_by="passthrough",
            method="unresolved",
            resolved_at=datetime(2024, 1, 15, tzinfo=UTC),
            reason="no identity resolver is active (passthrough)",
        ),
    )
    request = GateRequest(
        decision_id="dec-001",
        submission=gated_submission,
        gate_context=gate_context,
        budget_ms=1_000,
        identity=identity,
    )
    assert request.identity == identity
    # Optional for a request built from a bundle that predates DR-32.
    assert (
        GateRequest(
            decision_id="dec-001",
            submission=gated_submission,
            gate_context=gate_context,
            budget_ms=1_000,
        ).identity
        is None
    )
