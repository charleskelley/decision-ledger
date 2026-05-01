"""Behavioral tests for the universal GateVerdict and GateOutput contracts.

GateVerdict is the framework's enforcement-consumable verdict — universal
across gate kinds. GateOutput wraps it. Concrete subclass tests for the
LLM policy gate live under ``tests/core/gate/policy/``.
"""

import pytest
from pydantic import ValidationError

from core.actions import DecisionAction
from core.gate import GateOutput, GateVerdict


def _make_verdict(**overrides) -> GateVerdict:
    base = {
        "gate_id": "policy",
        "permitted_actions": [DecisionAction.ALLOW],
        "required_controls": [],
        "confidence": 0.85,
        "escalate_to_human": False,
        "escalation_reason": None,
    }
    base.update(overrides)
    return GateVerdict(**base)


def test_gate_verdict_minimal_construction():
    verdict = _make_verdict()
    assert verdict.gate_id == "policy"
    assert verdict.permitted_actions == [DecisionAction.ALLOW]


def test_gate_verdict_rejects_confidence_above_one():
    with pytest.raises(ValidationError):
        _make_verdict(confidence=1.1)


def test_gate_verdict_rejects_confidence_below_zero():
    with pytest.raises(ValidationError):
        _make_verdict(confidence=-0.1)


def test_gate_verdict_accepts_multiple_permitted_actions():
    verdict = _make_verdict(
        permitted_actions=[DecisionAction.ALLOW, DecisionAction.CHALLENGE],
        required_controls=["step_up_mfa"],
        confidence=0.75,
    )
    assert len(verdict.permitted_actions) == 2


def test_gate_verdict_with_escalation_flag_and_reason():
    verdict = _make_verdict(
        permitted_actions=[DecisionAction.HOLD],
        confidence=0.45,
        escalate_to_human=True,
        escalation_reason="Conflicting jurisdiction signals.",
    )
    assert verdict.escalate_to_human
    assert verdict.escalation_reason is not None


def test_gate_verdict_is_immutable():
    verdict = _make_verdict()
    with pytest.raises(ValidationError):
        verdict.confidence = 0.5


# ---------------------------------------------------------------------------
# GateOutput universal wrapper
# ---------------------------------------------------------------------------


def test_gate_output_with_verdict():
    verdict = _make_verdict()
    output = GateOutput(gate_id="policy", verdict=verdict)
    assert output.verdict is verdict
    assert output.gate_id == "policy"


def test_gate_output_verdict_may_be_none():
    # Schema-failure shape from the universal layer's perspective.
    output = GateOutput(gate_id="policy", verdict=None)
    assert output.verdict is None


def test_gate_output_is_immutable():
    output = GateOutput(gate_id="policy", verdict=None)
    with pytest.raises(ValidationError):
        output.verdict = _make_verdict()
