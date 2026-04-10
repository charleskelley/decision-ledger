"""Behavioral tests for PolicyGateOutput and Citation contracts.

PolicyGateOutput is the Pydantic validation boundary between the LLM and the
enforcement layer. Nothing unvalidated reaches enforcement. These tests verify
the field constraints that protect that boundary.
"""

import pytest
from pydantic import ValidationError

from core.actions import DecisionAction
from core.gate import PolicyGateOutput


def test_citation_construction(citation):
    assert citation.policy_id == "NIST-800-63B"
    assert len(citation.snippet) > 0
    assert len(citation.relevance) > 0


def test_citation_is_immutable(citation):
    with pytest.raises(ValidationError):
        citation.policy_id = "OTHER"


def test_policy_gate_output_construction(policy_gate_output):
    assert DecisionAction.ALLOW in policy_gate_output.permitted_actions
    assert policy_gate_output.confidence == 0.92
    assert not policy_gate_output.escalate_to_human


def test_policy_gate_output_rejects_confidence_above_one():
    with pytest.raises(ValidationError):
        PolicyGateOutput(
            permitted_actions=[DecisionAction.ALLOW],
            required_controls=[],
            rationale="Test.",
            citations=[],
            confidence=1.1,
            escalate_to_human=False,
            escalation_reason=None,
        )


def test_policy_gate_output_rejects_confidence_below_zero():
    with pytest.raises(ValidationError):
        PolicyGateOutput(
            permitted_actions=[DecisionAction.ALLOW],
            required_controls=[],
            rationale="Test.",
            citations=[],
            confidence=-0.1,
            escalate_to_human=False,
            escalation_reason=None,
        )


def test_policy_gate_output_accepts_all_decision_actions_as_permitted():
    output = PolicyGateOutput(
        permitted_actions=[DecisionAction.ALLOW, DecisionAction.CHALLENGE],
        required_controls=["step_up_mfa"],
        rationale="Moderate risk. Step-up authentication is sufficient.",
        citations=[],
        confidence=0.75,
        escalate_to_human=False,
        escalation_reason=None,
    )
    assert len(output.permitted_actions) == 2


def test_policy_gate_output_with_escalation_flag_and_reason():
    output = PolicyGateOutput(
        permitted_actions=[DecisionAction.HOLD],
        required_controls=["freeze_session"],
        rationale="Conflicting jurisdiction signals require human review.",
        citations=[],
        confidence=0.45,
        escalate_to_human=True,
        escalation_reason="GDPR vs US retention conflict detected in retrieved policy.",
    )
    assert output.escalate_to_human
    assert output.escalation_reason is not None


def test_policy_gate_output_escalate_to_human_without_reason_is_accepted():
    # The model does not enforce escalation_reason when escalate_to_human=True.
    # The enforcement layer is responsible for handling this case.
    output = PolicyGateOutput(
        permitted_actions=[DecisionAction.HOLD],
        required_controls=[],
        rationale="Uncertain.",
        citations=[],
        confidence=0.40,
        escalate_to_human=True,
        escalation_reason=None,
    )
    assert output.escalate_to_human
    assert output.escalation_reason is None


def test_policy_gate_output_is_immutable(policy_gate_output):
    with pytest.raises(ValidationError):
        policy_gate_output.confidence = 0.5
