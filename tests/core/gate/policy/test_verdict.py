"""Behavioral tests for PolicyGateVerdict — LLM-policy-gate verdict subclass.

Asserts that PolicyGateVerdict adds rationale + citations to the universal
GateVerdict, and that the gate_id discriminator is narrowed to "policy".
"""

import pytest
from pydantic import ValidationError

from core.actions import DecisionAction
from core.gate import GateVerdict
from core.gate.policy import Citation, PolicyGateVerdict


def _make(**overrides) -> PolicyGateVerdict:
    base = {
        "permitted_actions": [DecisionAction.ALLOW],
        "required_controls": [],
        "rationale": "Low risk profile.",
        "citations": [],
        "confidence": 0.9,
        "escalate_to_human": False,
        "escalation_reason": None,
    }
    base.update(overrides)
    return PolicyGateVerdict(**base)


def test_policy_gate_verdict_construction():
    verdict = _make()
    assert verdict.gate_id == "policy"
    assert verdict.rationale == "Low risk profile."


def test_policy_gate_verdict_with_citations():
    citation = Citation(
        policy_id="NIST-800-63B",
        snippet="Verifiers SHALL require MFA.",
        relevance="Supports MFA at AAL2.",
    )
    verdict = _make(citations=[citation])
    assert len(verdict.citations) == 1
    assert verdict.citations[0].policy_id == "NIST-800-63B"


def test_policy_gate_verdict_is_a_gate_verdict():
    verdict = _make()
    assert isinstance(verdict, GateVerdict)


def test_policy_gate_verdict_is_frozen():
    verdict = _make()
    with pytest.raises(ValidationError):
        verdict.rationale = "tampered"


def test_policy_gate_verdict_inherits_universal_validation():
    # confidence > 1.0 should fail per the universal GateVerdict constraint
    with pytest.raises(ValidationError):
        _make(confidence=1.5)
