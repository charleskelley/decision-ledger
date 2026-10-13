"""Behavioral tests for GateRequest — what the framework hands a gate.

GateRequest is the gate's whole view of a decision: the decision id, the
intake Observation (any reasoner's type, accepted through the protocol), the
GateContext addressed to the gate, and a positive budget.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.gate_request import GateRequest


def test_gate_request_carries_the_observation_and_its_gate_context(observation):
    request = GateRequest(
        decision_id="dec-001",
        observation=observation,
        gate_context=observation.gate_context,
        budget_ms=1_000,
    )
    assert request.observation is observation
    assert request.gate_context.gate_id == "policy"
    assert request.observation.reasoner_context is not None


def test_gate_request_rejects_an_object_that_is_not_an_observation(observation):
    with pytest.raises(ValidationError):
        GateRequest(
            decision_id="dec-001",
            observation={"event_id": "not-an-observation"},  # type: ignore[arg-type]
            gate_context=observation.gate_context,
            budget_ms=1_000,
        )


@pytest.mark.parametrize("budget_ms", [0, -5])
def test_gate_request_requires_a_positive_budget(observation, budget_ms):
    with pytest.raises(ValidationError):
        GateRequest(
            decision_id="dec-001",
            observation=observation,
            gate_context=observation.gate_context,
            budget_ms=budget_ms,
        )


def test_gate_request_is_frozen(observation):
    request = GateRequest(
        decision_id="dec-001",
        observation=observation,
        gate_context=observation.gate_context,
        budget_ms=1_000,
    )
    with pytest.raises(ValidationError):
        request.budget_ms = 5
