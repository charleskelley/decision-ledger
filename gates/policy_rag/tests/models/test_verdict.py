"""Behavioral tests for the policy gate's verdict schemas.

Asserts that PolicyGateVerdict adds rationale + citations to the universal
GateVerdict, that the gate_id discriminator is narrowed to "policy", that
its response schema is exactly what it was before DR-34, and that
TieredPolicyGateVerdict adds the required conflict/gap report.
"""

import hashlib
import json

import pytest
from pydantic import ValidationError

from core.actions import DecisionAction
from core.gate import GateVerdict
from policy_rag_gate.models import Citation, PolicyGateVerdict, TieredPolicyGateVerdict


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


def test_universal_returns_a_plain_gate_verdict_with_the_enforcement_fields():
    # The bundle stores only the universal verdict (DR-28); the rationale and
    # citations travel in the gate's extra instead.
    verdict = _make(
        permitted_actions=[DecisionAction.CHALLENGE, DecisionAction.HOLD],
        required_controls=["step_up_mfa"],
        confidence=0.7,
        escalate_to_human=True,
        escalation_reason="Needs review.",
    )

    universal = verdict.universal()

    assert type(universal) is GateVerdict
    assert universal.gate_id == "policy"
    assert universal.permitted_actions == [
        DecisionAction.CHALLENGE,
        DecisionAction.HOLD,
    ]
    assert universal.required_controls == ["step_up_mfa"]
    assert universal.confidence == 0.7
    assert universal.escalate_to_human is True
    assert universal.escalation_reason == "Needs review."
    assert not hasattr(universal, "rationale")


# ---------------------------------------------------------------------------
# The response schemas the model receives (DR-34)
# ---------------------------------------------------------------------------

#: SHA-256 of ``PolicyGateVerdict.model_json_schema()`` (keys sorted) as it was
#: before DR-34. The structured-output SDK sends the class's name, docstring,
#: and fields to the model, so a template written before DR-34 must keep
#: getting exactly this schema: a change here changes old templates' behavior.
_PRE_DR34_SCHEMA_SHA256 = (
    "5cf0d580ac75a99202a370fed2dbc0718e97f0fa309d6a4be513f08995fd5a9b"
)


def test_the_numbered_list_response_schema_is_unchanged_since_before_dr34():
    schema = json.dumps(PolicyGateVerdict.model_json_schema(), sort_keys=True)

    assert hashlib.sha256(schema.encode()).hexdigest() == _PRE_DR34_SCHEMA_SHA256
    assert PolicyGateVerdict.__name__ == "PolicyGateVerdict"


def _tiered(**overrides) -> TieredPolicyGateVerdict:
    base = {
        "permitted_actions": [DecisionAction.CHALLENGE],
        "required_controls": [],
        "rationale": "Tier 1 governs.",
        "citations": [],
        "confidence": 0.8,
        "escalate_to_human": False,
        "escalation_reason": None,
        "governing_corpus_id": "regulatory",
        "conflict_detected": False,
        "evidence_sufficient": True,
    }
    base.update(overrides)
    return TieredPolicyGateVerdict(**base)


def test_the_tiered_verdict_adds_the_conflict_gap_report():
    verdict = _tiered(governing_corpus_id=None, evidence_sufficient=False)

    assert isinstance(verdict, PolicyGateVerdict)
    assert verdict.governing_corpus_id is None
    assert verdict.evidence_sufficient is False
    assert type(verdict.universal()) is GateVerdict


@pytest.mark.parametrize(
    "missing", ["governing_corpus_id", "conflict_detected", "evidence_sufficient"]
)
def test_the_tiered_report_is_required_of_the_model(missing):
    fields = _tiered().model_dump()
    del fields[missing]

    with pytest.raises(ValidationError, match=missing):
        TieredPolicyGateVerdict.model_validate(fields)


def test_the_tiered_report_fields_are_strict():
    with pytest.raises(ValidationError):
        _tiered(conflict_detected="no")


def test_the_tiered_schema_requires_every_report_field():
    schema = TieredPolicyGateVerdict.model_json_schema()

    assert {"governing_corpus_id", "conflict_detected", "evidence_sufficient"} <= set(
        schema["required"]
    )
