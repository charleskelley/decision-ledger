"""Behavioral tests for PolicyGateOutput — LLM-policy-gate output subclass.

Covers response_text, token_cost, the schema-failure shape (verdict=None,
response_text populated), and the IS-A relationship with GateOutput.
"""

import pytest
from pydantic import ValidationError

from core.actions import DecisionAction
from core.gate import GateOutput
from core.gate.policy import PolicyGateOutput, PolicyGateVerdict, TokenCost


def _verdict() -> PolicyGateVerdict:
    return PolicyGateVerdict(
        permitted_actions=[DecisionAction.ALLOW],
        required_controls=[],
        rationale="Low risk.",
        citations=[],
        confidence=0.9,
        escalate_to_human=False,
        escalation_reason=None,
    )


def _token_cost() -> TokenCost:
    return TokenCost(
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        cost_usd=0.001,
        model="gpt-4o",
    )


def test_policy_gate_output_with_verdict():
    output = PolicyGateOutput(
        verdict=_verdict(),
        response_text='{"permitted_actions": ["ALLOW"]}',
        token_cost=_token_cost(),
    )
    assert output.gate_id == "policy"
    assert output.verdict is not None
    assert output.response_text is not None
    assert output.token_cost is not None


def test_policy_gate_output_schema_failure_shape():
    # The load-bearing case: verdict=None but response_text populated for
    # forensic HOLD review.
    output = PolicyGateOutput(
        verdict=None,
        response_text="not valid json",
    )
    assert output.verdict is None
    assert output.response_text == "not valid json"
    assert output.token_cost is None


def test_policy_gate_output_token_cost_optional():
    output = PolicyGateOutput(verdict=_verdict())
    assert output.token_cost is None


def test_policy_gate_output_is_a_gate_output():
    output = PolicyGateOutput(verdict=_verdict())
    assert isinstance(output, GateOutput)


def test_policy_gate_output_is_frozen():
    output = PolicyGateOutput(verdict=_verdict())
    with pytest.raises(ValidationError):
        output.response_text = "tampered"
