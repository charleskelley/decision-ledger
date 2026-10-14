"""Behavioral tests for RulesGate — deterministic evaluation of a rule set.

Pins what the gate returns for each outcome: the universal ``GateOutput``
with ``deterministic=True``; a verdict permitting every matching rule's
action (or the default) with the matching rules' controls; evidence citing
the matching rules (never jurisdictions); a self-contained snapshot; and a
fail-closed ``verdict=None`` with the reason recorded when the rule set is
unknown, belongs to another reasoner, or cannot evaluate.
"""

from __future__ import annotations

import pytest

from core.actions import DecisionAction
from core.gate import EvidenceRef, GateOutput
from rules_gate import RulesGate, RulesGateSnapshot, parse_ruleset


@pytest.fixture
def gate(ruleset_data) -> RulesGate:
    ruleset = parse_ruleset(ruleset_data)
    return RulesGate({ruleset.ruleset_id: ruleset})


async def _evaluate(gate, observation_for, request_for, **kwargs) -> GateOutput:
    return await gate.evaluate(request_for(observation_for(**kwargs)))


def _snapshot(output: GateOutput) -> RulesGateSnapshot:
    return RulesGateSnapshot.model_validate(output.evidence_snapshot, strict=False)


async def test_no_matching_rule_permits_the_default_action(
    gate, observation_for, request_for
):
    output = await _evaluate(gate, observation_for, request_for)

    assert type(output) is GateOutput
    assert output.gate_id == "rules"
    assert output.deterministic is True
    assert output.verdict is not None
    assert output.verdict.permitted_actions == [DecisionAction.ALLOW]
    assert output.verdict.required_controls == []
    assert output.verdict.confidence == 1.0
    assert output.evidence == (
        EvidenceRef(source_id="widget-guardrails@1.0.0", locator="default_action"),
    )
    snapshot = _snapshot(output)
    assert snapshot.default_applied is True
    assert snapshot.matched_rule_ids == []


async def test_a_matching_rule_permits_its_action_and_requires_its_controls(
    gate, observation_for, request_for
):
    output = await _evaluate(gate, observation_for, request_for, thin_history=True)

    assert output.verdict is not None
    assert output.verdict.permitted_actions == [DecisionAction.HOLD]
    assert output.verdict.required_controls == ["calibration_review"]
    assert output.evidence == (
        EvidenceRef(source_id="widget-guardrails@1.0.0", locator="thin-history-hold"),
    )
    assert _snapshot(output).matched_rule_ids == ["thin-history-hold"]


async def test_every_matching_rule_contributes_its_action_and_controls(
    gate, observation_for, request_for
):
    output = await _evaluate(
        gate,
        observation_for,
        request_for,
        label_value=0.97,
        thin_history=True,
        region="REGION_B",
        wobble=7,
    )

    assert output.verdict is not None
    assert output.verdict.permitted_actions == [
        DecisionAction.HOLD,
        DecisionAction.BLOCK,
        DecisionAction.CHALLENGE,
    ]
    assert output.verdict.required_controls == ["calibration_review", "notify_owner"]
    assert [e.locator for e in output.evidence] == [
        "thin-history-hold",
        "extreme-score-block",
        "wobbly-region-challenge",
    ]


async def test_rule_evidence_carries_no_jurisdictions(
    gate, observation_for, request_for
):
    output = await _evaluate(gate, observation_for, request_for, label_value=0.97)

    assert output.evidence_jurisdictions() == frozenset()


async def test_the_snapshot_records_the_ruleset_and_every_observed_field(
    gate, observation_for, request_for, ruleset_data
):
    output = await _evaluate(gate, observation_for, request_for, label_value=0.42)

    snapshot = _snapshot(output)
    assert snapshot.ruleset_id == "widget-guardrails"
    assert parse_ruleset(snapshot.ruleset) == parse_ruleset(ruleset_data)
    assert snapshot.observed == {
        "feature_set.region": "REGION_C",
        "feature_set.thin_history": False,
        "feature_set.wobble": 1,
        "label_value": 0.42,
    }


async def test_an_unknown_ruleset_fails_closed(gate, observation_for, request_for):
    output = await _evaluate(
        gate, observation_for, request_for, ruleset_id="no-such-set"
    )

    assert output.verdict is None
    assert output.deterministic is True
    assert output.extra["failure"] == "unknown_ruleset"
    assert _snapshot(output).ruleset is None


async def test_a_ruleset_registered_for_another_reasoner_fails_closed(
    gate, observation_for, request_for
):
    output = await _evaluate(
        gate, observation_for, request_for, reasoner_id="other-reasoner"
    )

    assert output.verdict is None
    assert output.extra["failure"] == "ruleset_reasoner_mismatch"
    assert "other-reasoner" in str(output.extra["detail"])
    # The owning tenant's rules (and its id) never land in the caller's bundle.
    assert _snapshot(output).ruleset is None
    assert "widget-reasoner" not in str(output.model_dump(mode="json"))


async def test_a_predicate_that_cannot_evaluate_fails_closed(
    observation_for, request_for, ruleset_data
):
    # The first rule reads feature_set.thin_history; drop it from the subject.
    gate = RulesGate({"widget-guardrails": parse_ruleset(ruleset_data)})
    observation = observation_for()
    assert observation.reasoner_context is not None
    features = dict(observation.reasoner_context.feature_set)
    del features["thin_history"]
    observation = observation.model_copy(
        update={
            "reasoner_context": observation.reasoner_context.model_copy(
                update={"feature_set": features}
            )
        }
    )

    output = await gate.evaluate(request_for(observation))

    assert output.verdict is None
    assert output.extra["failure"] == "predicate_error"
    assert "thin_history" in str(output.extra["detail"])


async def test_evaluation_is_deterministic(gate, observation_for, request_for):
    observation = observation_for(label_value=0.97, thin_history=True)

    first = await gate.evaluate(request_for(observation))
    second = await gate.evaluate(request_for(observation))

    assert first == second


def test_the_gate_exposes_its_registered_ruleset_ids(gate):
    assert gate.ruleset_ids == ["widget-guardrails"]
    gate.close()  # a no-op: the gate opens nothing
