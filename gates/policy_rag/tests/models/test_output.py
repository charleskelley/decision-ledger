"""Behavioral tests for the policy gate's output extension — PolicyGateExtra.

``PolicyGateExtra`` replaces the ``PolicyGateOutput`` subclass (DR-28): the
gate returns the universal ``GateOutput`` and records its rationale,
citations, raw response, token cost, retrieval latency, and failing stage in
``GateOutput.extra``. Covers the success and no-verdict shapes and the JSON
round trip through the opaque extension.
"""

import json

import pytest
from pydantic import ValidationError

from core.actions import DecisionAction
from core.gate import GateOutput, GateVerdict
from policy_rag_gate.models import Citation, PolicyGateExtra, TokenCost


def _token_cost() -> TokenCost:
    return TokenCost(
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        cost_usd=0.001,
        model="gpt-4o",
    )


def test_extra_with_a_verdict_carries_the_explanation_and_cost():
    extra = PolicyGateExtra(
        rationale="Low risk.",
        citations=[
            Citation(policy_id="STUB-POLICY-A", snippet="text", relevance="why")
        ],
        token_cost=_token_cost(),
        retrieval_ms=12.5,
    )
    assert extra.rationale == "Low risk."
    assert extra.citations[0].policy_id == "STUB-POLICY-A"
    assert extra.token_cost is not None
    assert extra.failure is None


def test_extra_without_a_verdict_records_the_failing_stage():
    extra = PolicyGateExtra(failure="llm_error", retrieval_ms=3.0)
    assert extra.rationale is None
    assert extra.citations == []
    assert extra.token_cost is None
    assert extra.failure == "llm_error"


def test_extra_rejects_an_unknown_failure_stage():
    with pytest.raises(ValidationError):
        PolicyGateExtra(failure="cosmic_ray")  # type: ignore[arg-type]


def test_extra_round_trips_through_the_gate_output_extension():
    extra = PolicyGateExtra(
        rationale="ok",
        citations=[Citation(policy_id="P", snippet="s", relevance="r")],
        token_cost=_token_cost(),
        retrieval_ms=1.5,
    )
    output = GateOutput(
        gate_id="policy",
        deterministic=False,
        verdict=None,
        extra=extra.model_dump(mode="json"),
    )
    stored = json.loads(json.dumps(output.model_dump(mode="json")))
    reloaded = GateOutput.model_validate(stored, strict=False)

    assert PolicyGateExtra.model_validate(reloaded.extra, strict=False) == extra


def test_token_cost_rejects_negative_tokens():
    with pytest.raises(ValidationError):
        TokenCost(
            prompt_tokens=-1,
            completion_tokens=0,
            total_tokens=0,
            cost_usd=0.0,
            model="m",
        )


def test_extra_is_frozen():
    extra = PolicyGateExtra()
    with pytest.raises(ValidationError):
        extra.rationale = "tampered"


def test_extra_records_the_tiered_report_and_the_verdict_a_hold_replaced():
    returned = GateVerdict(
        gate_id="policy",
        permitted_actions=[DecisionAction.ALLOW],
        required_controls=[],
        confidence=0.7,
        escalate_to_human=False,
        escalation_reason=None,
    )
    extra = PolicyGateExtra(
        rationale="Passages disagree.",
        governing_corpus_id="regulatory",
        conflict_detected=True,
        evidence_sufficient=True,
        protocol_hold="conflict_detected",
        verdict_as_returned=returned,
    )

    stored = json.loads(json.dumps(extra.model_dump(mode="json")))

    assert PolicyGateExtra.model_validate(stored, strict=False) == extra


def test_extra_of_a_numbered_list_template_has_no_tiered_report():
    extra = PolicyGateExtra(rationale="ok")

    assert extra.governing_corpus_id is None
    assert extra.conflict_detected is None
    assert extra.evidence_sufficient is None
    assert extra.protocol_hold is None
    assert extra.verdict_as_returned is None


def test_extra_rejects_an_unknown_hold():
    with pytest.raises(ValidationError):
        PolicyGateExtra(protocol_hold="authority_violation")  # type: ignore[arg-type]
