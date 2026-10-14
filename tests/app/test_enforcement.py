"""Behavioral tests for the deterministic enforcement resolver.

Tests cover every tier trigger path, the gate-level / decision-level split
(DR-28), the deterministic-gate skips, the evidence-gated jurisdiction tier,
and how the decision-level tier combines with the gate-level result. These
are the governance guarantees — exhaustive coverage is intentional.

Fixture baseline: observation (FAST_PATH_ALLOW, reasoner_context populated),
gate_output (policy gate, non-deterministic, verdict ALLOW at confidence 0.92,
one ``REGION_A`` evidence reference — so the jurisdiction tier runs and finds
no conflict). Each test deviates minimally from the baseline to isolate the
trigger under test.

Tiers 3 and 5 read their profile from the ``ReasonerRegistration`` passed to
``resolve()``. The baseline registration (``_REG``) declares a novel-entity
feature and two conflict groups; Tier 3/5 tests vary the profile to show
that the registration — not a framework constant — drives the behavior.

``test_split_matches_the_single_pass_resolver_*`` pins the split against a
transcription of the pre-split single-pass resolver: for the retrieving LLM
gate every combination yields the same action and credited rule, except the
one DR-28 rule that changes outcomes — the jurisdiction tier no longer runs
when the gate's evidence carries no jurisdictions (empty retrieval).
"""

from __future__ import annotations

import itertools

import pytest

from app.enforcement.resolver import (
    ADVERSARIAL_CONTROL,
    JURISDICTION_CONFLICT_CTL,
    LOW_CONFIDENCE_THRESHOLD,
    resolve,
    resolve_gate_output,
)
from core.actions import DecisionAction
from core.gate import EvidenceRef, GateOutput
from core.observation import ReasonerRegistration
from core.routes import GateRoute

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DECISION_ID = "dec-test-001"
_NOVEL_FEATURE = "thin_history"
_GROUP_A = frozenset({"REGION_A", "REGION_A_COAST"})
_GROUP_B = frozenset({"REGION_B"})
_NOVEL_REASON = (
    "tier3_novel_entity: fired — Insufficient entity history for confident "
    "decision — HOLD for review"
)


def _registration(
    *,
    novel_entity_feature: str | None = _NOVEL_FEATURE,
    jurisdiction_conflict_groups: tuple[frozenset[str], ...] = (_GROUP_A, _GROUP_B),
) -> ReasonerRegistration:
    return ReasonerRegistration(
        reasoner_id="stub-reasoner",
        reasoner_name="Test Reasoner",
        allowed_gate_ids=frozenset({"policy"}),
        novel_entity_feature=novel_entity_feature,
        jurisdiction_conflict_groups=jurisdiction_conflict_groups,
    )


#: Baseline registration — full enforcement profile. Shaped like the stub
#: reasoner's, but with a two-member group so the within-group Tier 5 case is
#: testable.
_REG = _registration()


def _gate_route(observation):
    return observation.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )


def _with_feature(observation, reasoner_context, name: str, value: object):
    rc = reasoner_context.model_copy(
        update={"feature_set": {**reasoner_context.feature_set, name: value}}
    )
    return _gate_route(observation).model_copy(update={"reasoner_context": rc})


def _verdict(gate_output: GateOutput, **updates) -> GateOutput:
    """The baseline output with its verdict's fields replaced."""
    assert gate_output.verdict is not None
    return gate_output.model_copy(
        update={"verdict": gate_output.verdict.model_copy(update=updates)}
    )


def _evidence(gate_output: GateOutput, *jurisdictions: str) -> GateOutput:
    """The baseline output citing one source per jurisdiction."""
    refs = tuple(
        EvidenceRef(source_id=f"doc-{j}", jurisdictions=frozenset({j}))
        for j in jurisdictions
    )
    return gate_output.model_copy(update={"evidence": refs})


def _resolve(obs, gate_output, registration=_REG):
    return resolve(
        obs, gate_output, decision_id=_DECISION_ID, registration=registration
    )


# ---------------------------------------------------------------------------
# Fast-path short-circuits
# ---------------------------------------------------------------------------


def test_fast_path_allow_returns_allow_without_tier_evaluation(
    observation, gate_output
):
    # observation fixture has route=FAST_PATH_ALLOW
    decision = _resolve(observation, gate_output)
    assert decision.decision_action == DecisionAction.ALLOW
    assert decision.enforcement_rule_applied is None
    assert decision.override_log == [
        "fast_path_allow: FAST_PATH_ALLOW — policy gate skipped"
    ]


def test_fast_path_allow_ignores_gate_output_none(observation):
    decision = _resolve(observation, None)
    assert decision.decision_action == DecisionAction.ALLOW


def test_fast_path_block_returns_block(observation, gate_output):
    obs = observation.model_copy(
        update={
            "route": GateRoute.FAST_PATH_BLOCK,
            "fast_path_rationale": "widget_score=0.97 > 0.95 → FAST_PATH_BLOCK",
        }
    )
    decision = _resolve(obs, gate_output)
    assert decision.decision_action == DecisionAction.BLOCK
    assert decision.override_log == [
        "fast_path_block: FAST_PATH_BLOCK — policy gate skipped"
    ]


def test_fast_path_does_not_evaluate_the_novel_entity_tier(
    observation, reasoner_context
):
    obs = _with_feature(observation, reasoner_context, _NOVEL_FEATURE, True)
    obs = obs.model_copy(
        update={"route": GateRoute.FAST_PATH_ALLOW, "fast_path_rationale": "low"}
    )
    decision = _resolve(obs, None)
    assert decision.decision_action == DecisionAction.ALLOW


# ---------------------------------------------------------------------------
# Tier 1: Schema failure → HOLD
# ---------------------------------------------------------------------------


def test_tier1_schema_failure_holds_when_verdict_is_none(observation, gate_output):
    decision = _resolve(
        _gate_route(observation), gate_output.model_copy(update={"verdict": None})
    )
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier1_schema_failure"
    assert decision.override_log[0] == (
        "tier1_schema_failure: fired — Gate output failed schema validation — "
        "routed to HOLD for review"
    )


def test_tier1_holds_when_no_gate_output_reaches_enforcement_on_the_gate_route(
    observation,
):
    decision = _resolve(_gate_route(observation), None)
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier1_schema_failure"


def test_tier1_applies_to_a_deterministic_gate_too(observation, gate_output):
    rules_failure = gate_output.model_copy(
        update={"verdict": None, "deterministic": True}
    )
    decision = _resolve(_gate_route(observation), rules_failure)
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier1_schema_failure"


# ---------------------------------------------------------------------------
# Tier 2: Adversarial probe → BLOCK (non-deterministic gates only)
# ---------------------------------------------------------------------------


def test_tier2_adversarial_probe_blocks_when_control_present(observation, gate_output):
    adversarial = _verdict(gate_output, required_controls=[ADVERSARIAL_CONTROL])
    decision = _resolve(_gate_route(observation), adversarial)
    assert decision.decision_action == DecisionAction.BLOCK
    assert decision.enforcement_rule_applied == "tier2_adversarial_probe"


def test_tier2_does_not_fire_without_adversarial_control(observation, gate_output):
    decision = _resolve(_gate_route(observation), gate_output)
    assert decision.enforcement_rule_applied != "tier2_adversarial_probe"
    assert "tier2_adversarial_probe: not fired" in decision.override_log


def test_tier2_is_skipped_for_a_deterministic_gate(observation, gate_output):
    # A rules or lookup gate cannot be prompt-injected; the sentinel is an
    # LLM-gate signal, so a deterministic gate's control list does not BLOCK.
    deterministic = _verdict(
        gate_output, required_controls=[ADVERSARIAL_CONTROL]
    ).model_copy(update={"deterministic": True})
    decision = _resolve(_gate_route(observation), deterministic)
    assert decision.decision_action == DecisionAction.ALLOW
    assert decision.enforcement_rule_applied is None
    assert "tier2_adversarial_probe: skipped — deterministic gate" in (
        decision.override_log
    )


# ---------------------------------------------------------------------------
# Tier 3: Novel entity (decision-level, registration-declared feature) → HOLD
# ---------------------------------------------------------------------------


def test_tier3_holds_when_registered_novel_entity_feature_is_true(
    observation, gate_output, reasoner_context
):
    obs = _with_feature(observation, reasoner_context, _NOVEL_FEATURE, True)
    decision = _resolve(obs, gate_output)
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier3_novel_entity"


def test_tier3_does_not_fire_when_history_sufficient(observation, gate_output):
    decision = _resolve(_gate_route(observation), gate_output)
    assert decision.enforcement_rule_applied != "tier3_novel_entity"
    assert "tier3_novel_entity: not fired — history sufficient" in (
        decision.override_log
    )


def test_tier3_never_fires_when_registration_has_no_novel_entity_feature(
    observation, gate_output, reasoner_context
):
    # The feature is present and truthy, but the registration declares no
    # novel-entity feature, so the tier is disabled for this reasoner.
    obs = _with_feature(observation, reasoner_context, _NOVEL_FEATURE, True)
    decision = _resolve(obs, gate_output, _registration(novel_entity_feature=None))
    assert decision.enforcement_rule_applied != "tier3_novel_entity"
    assert decision.decision_action == DecisionAction.ALLOW
    assert (
        "tier3_novel_entity: not fired — no novel-entity feature configured"
        in decision.override_log
    )


def test_tier3_reads_the_feature_name_declared_on_the_registration(
    observation, gate_output, reasoner_context
):
    # A reasoner that calls its novelty signal something else still gets Tier 3,
    # keyed on its own feature name.
    obs = _with_feature(observation, reasoner_context, "fresh_entity", True)
    custom = _registration(novel_entity_feature="fresh_entity")
    decision = _resolve(obs, gate_output, custom)
    assert decision.enforcement_rule_applied == "tier3_novel_entity"


def test_tier3_ignores_truthy_feature_under_a_different_name(
    observation, gate_output, reasoner_context
):
    obs = _with_feature(observation, reasoner_context, "fresh_entity", True)
    decision = _resolve(obs, gate_output)
    assert decision.enforcement_rule_applied != "tier3_novel_entity"


def test_tier3_hold_reason_names_the_entity_not_an_account(
    observation, gate_output, reasoner_context
):
    # The DR-26 parity wording ("account history") is retired deliberately
    # with the tier split: the subject of a decision is an entity (DR-12).
    obs = _with_feature(observation, reasoner_context, _NOVEL_FEATURE, True)
    decision = _resolve(obs, gate_output)
    assert decision.override_log[-1] == _NOVEL_REASON


def test_tier3_is_evaluated_once_per_decision_after_the_gate_tiers(
    observation, gate_output, reasoner_context
):
    obs = _with_feature(observation, reasoner_context, _NOVEL_FEATURE, True)
    decision = _resolve(obs, gate_output)
    tier3 = [e for e in decision.override_log if e.startswith("tier3_")]
    assert tier3 == [_NOVEL_REASON]
    assert decision.override_log[-1] == _NOVEL_REASON


# ---------------------------------------------------------------------------
# Tier 4: Low confidence + high-risk action → HOLD (non-deterministic only)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", [DecisionAction.BLOCK, DecisionAction.HOLD])
def test_tier4_holds_when_low_confidence_and_high_risk_action(
    observation, gate_output, action
):
    low = _verdict(
        gate_output,
        confidence=LOW_CONFIDENCE_THRESHOLD - 0.01,
        permitted_actions=[action],
    )
    decision = _resolve(_gate_route(observation), low)
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier4_low_confidence"


@pytest.mark.parametrize("action", [DecisionAction.ALLOW, DecisionAction.CHALLENGE])
def test_tier4_does_not_fire_when_low_confidence_but_low_risk_action(
    observation, gate_output, action
):
    low = _verdict(
        gate_output,
        confidence=LOW_CONFIDENCE_THRESHOLD - 0.01,
        permitted_actions=[action],
    )
    decision = _resolve(_gate_route(observation), low)
    assert decision.enforcement_rule_applied != "tier4_low_confidence"
    assert decision.decision_action == action


def test_tier4_does_not_fire_when_confidence_at_threshold(observation, gate_output):
    at_threshold = _verdict(
        gate_output,
        confidence=LOW_CONFIDENCE_THRESHOLD,
        permitted_actions=[DecisionAction.BLOCK],
    )
    decision = _resolve(_gate_route(observation), at_threshold)
    assert decision.enforcement_rule_applied != "tier4_low_confidence"
    assert decision.decision_action == DecisionAction.BLOCK


def test_tier4_is_skipped_for_a_deterministic_gate(observation, gate_output):
    # A deterministic gate's confidence is not a calibrated self-report; its
    # BLOCK stands.
    rules_block = _verdict(
        gate_output, confidence=0.0, permitted_actions=[DecisionAction.BLOCK]
    ).model_copy(update={"deterministic": True})
    decision = _resolve(_gate_route(observation), rules_block)
    assert decision.decision_action == DecisionAction.BLOCK
    assert decision.enforcement_rule_applied is None
    assert "tier4_low_confidence: skipped — deterministic gate" in (
        decision.override_log
    )


# ---------------------------------------------------------------------------
# Tier 5: Jurisdiction conflict (evidence-gated, registration groups) → HOLD
# ---------------------------------------------------------------------------


def test_tier5_holds_when_jurisdiction_conflict_control_present(
    observation, gate_output
):
    conflict = _verdict(gate_output, required_controls=[JURISDICTION_CONFLICT_CTL])
    decision = _resolve(_gate_route(observation), conflict)
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier5_jurisdiction_conflict"


def test_tier5_holds_when_escalation_reason_names_a_conflict(observation, gate_output):
    escalated = _verdict(
        gate_output,
        escalate_to_human=True,
        escalation_reason="Conflict between regional requirements.",
    )
    decision = _resolve(_gate_route(observation), escalated)
    assert decision.enforcement_rule_applied == "tier5_jurisdiction_conflict"


def test_tier5_control_sentinel_fires_even_with_empty_conflict_groups(
    observation, gate_output
):
    # The gate's explicit signal does not depend on the structural profile.
    conflict = _verdict(gate_output, required_controls=[JURISDICTION_CONFLICT_CTL])
    no_groups = _registration(jurisdiction_conflict_groups=())
    decision = _resolve(_gate_route(observation), conflict, no_groups)
    assert decision.enforcement_rule_applied == "tier5_jurisdiction_conflict"


def test_tier5_holds_when_evidence_spans_two_conflict_groups(observation, gate_output):
    output = _evidence(gate_output, "REGION_A", "REGION_B")
    decision = _resolve(_gate_route(observation), output)
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier5_jurisdiction_conflict"


def test_tier5_reads_jurisdictions_from_every_reference(observation, gate_output):
    # One reference carrying both jurisdictions is as conflicting as two.
    output = gate_output.model_copy(
        update={
            "evidence": (
                EvidenceRef(
                    source_id="doc-multi",
                    jurisdictions=frozenset({"REGION_A", "REGION_B"}),
                ),
            )
        }
    )
    decision = _resolve(_gate_route(observation), output)
    assert decision.enforcement_rule_applied == "tier5_jurisdiction_conflict"


def test_tier5_does_not_fire_when_evidence_touches_one_group(observation, gate_output):
    # Two jurisdictions, but both inside _GROUP_A — not a conflict.
    output = _evidence(gate_output, "REGION_A", "REGION_A_COAST")
    decision = _resolve(_gate_route(observation), output)
    assert decision.enforcement_rule_applied != "tier5_jurisdiction_conflict"
    assert "tier5_jurisdiction_conflict: not fired" in decision.override_log


def test_tier5_structural_check_never_fires_with_empty_conflict_groups(
    observation, gate_output
):
    output = _evidence(gate_output, "REGION_A", "REGION_B")
    no_groups = _registration(jurisdiction_conflict_groups=())
    decision = _resolve(_gate_route(observation), output, no_groups)
    assert decision.enforcement_rule_applied != "tier5_jurisdiction_conflict"
    assert decision.decision_action == DecisionAction.ALLOW


def test_tier5_ignores_jurisdictions_outside_every_group(observation, gate_output):
    # INTERNAL belongs to no group; REGION_B touches exactly one → no conflict.
    output = _evidence(gate_output, "INTERNAL", "REGION_B")
    decision = _resolve(_gate_route(observation), output)
    assert decision.enforcement_rule_applied != "tier5_jurisdiction_conflict"


def test_tier5_uses_the_groups_declared_on_the_registration(observation, gate_output):
    # A reasoner with its own regional split gets conflict detection on its terms.
    regional = _registration(
        jurisdiction_conflict_groups=(frozenset({"ZONE_X"}), frozenset({"ZONE_Y"}))
    )
    output = _evidence(gate_output, "ZONE_X", "ZONE_Y")
    decision = _resolve(_gate_route(observation), output, regional)
    assert decision.enforcement_rule_applied == "tier5_jurisdiction_conflict"


def test_tier5_treats_duplicate_groups_as_one(observation, gate_output):
    # The same group listed twice is still one group — no conflict.
    dup = _registration(jurisdiction_conflict_groups=(_GROUP_A, _GROUP_A))
    output = _evidence(gate_output, "REGION_A", "REGION_A_COAST")
    decision = _resolve(_gate_route(observation), output, dup)
    assert decision.enforcement_rule_applied != "tier5_jurisdiction_conflict"


def test_tier5_is_skipped_when_evidence_carries_no_jurisdictions(
    observation, gate_output
):
    # DR-28: the tier runs only when the gate's evidence carries jurisdictions
    # — a gate that deals in none (rules, lookups) cannot conflict across them,
    # even if its control list happens to carry the sentinel.
    output = _verdict(
        gate_output, required_controls=[JURISDICTION_CONFLICT_CTL]
    ).model_copy(update={"evidence": (EvidenceRef(source_id="ruleset@1"),)})
    decision = _resolve(_gate_route(observation), output)
    assert decision.enforcement_rule_applied != "tier5_jurisdiction_conflict"
    assert decision.decision_action == DecisionAction.ALLOW
    assert (
        "tier5_jurisdiction_conflict: skipped — gate evidence carries no "
        "jurisdictions" in decision.override_log
    )


def test_tier5_runs_for_a_deterministic_gate_whose_evidence_has_jurisdictions(
    observation, gate_output
):
    output = _evidence(gate_output, "REGION_A", "REGION_B").model_copy(
        update={"deterministic": True}
    )
    decision = _resolve(_gate_route(observation), output)
    assert decision.enforcement_rule_applied == "tier5_jurisdiction_conflict"


# ---------------------------------------------------------------------------
# Tier 6: Default — accept gate output
# ---------------------------------------------------------------------------


def test_tier6_accepts_allow_from_gate(observation, gate_output):
    decision = _resolve(_gate_route(observation), gate_output)
    assert decision.decision_action == DecisionAction.ALLOW
    assert decision.enforcement_rule_applied is None


def test_tier6_selects_most_conservative_from_multiple_permitted_actions(
    observation, gate_output
):
    multi = _verdict(
        gate_output,
        permitted_actions=[DecisionAction.ALLOW, DecisionAction.CHALLENGE],
    )
    decision = _resolve(_gate_route(observation), multi)
    assert decision.decision_action == DecisionAction.CHALLENGE


def test_tier6_passes_hold_through_without_override_rule(observation, gate_output):
    # Gate-accepted HOLD: the gate itself recommended HOLD; no override rule fires.
    # Realized outcome will come from the resolution attempt log post-decision.
    hold = _verdict(gate_output, permitted_actions=[DecisionAction.HOLD])
    decision = _resolve(_gate_route(observation), hold)
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied is None  # gate accepted, not overridden


def test_tier6_empty_permitted_actions_defaults_to_hold(observation, gate_output):
    empty = _verdict(gate_output, permitted_actions=[])
    decision = _resolve(_gate_route(observation), empty)
    assert decision.decision_action == DecisionAction.HOLD


# ---------------------------------------------------------------------------
# Combination — decision-level Tier 3 with the gate-level result
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gate_action",
    [
        DecisionAction.ALLOW,
        DecisionAction.CHALLENGE,
        DecisionAction.HOLD,
        DecisionAction.BLOCK,
    ],
)
def test_tier3_holds_any_accepted_gate_verdict_for_review(
    observation, gate_output, reasoner_context, gate_action
):
    # DR-6: a novel entity routes to human review, whatever the gate would
    # have let through or blocked — a confident BLOCK included.
    obs = _with_feature(observation, reasoner_context, _NOVEL_FEATURE, True)
    decision = _resolve(obs, _verdict(gate_output, permitted_actions=[gate_action]))
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier3_novel_entity"
    assert decision.override_log[-1] == _NOVEL_REASON


def test_tier2_block_outranks_tier3(observation, gate_output, reasoner_context):
    obs = _with_feature(observation, reasoner_context, _NOVEL_FEATURE, True)
    adversarial = _verdict(gate_output, required_controls=[ADVERSARIAL_CONTROL])
    decision = _resolve(obs, adversarial)
    assert decision.decision_action == DecisionAction.BLOCK
    assert decision.enforcement_rule_applied == "tier2_adversarial_probe"
    assert decision.override_log[-1] == (
        f"{_NOVEL_REASON}; tier2_adversarial_probe outranks it"
    )


def test_tier1_outranks_tier3(observation, gate_output, reasoner_context):
    obs = _with_feature(observation, reasoner_context, _NOVEL_FEATURE, True)
    decision = _resolve(obs, gate_output.model_copy(update={"verdict": None}))
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier1_schema_failure"
    assert decision.override_log[-1] == (
        f"{_NOVEL_REASON}; tier1_schema_failure outranks it"
    )


@pytest.mark.parametrize(
    "gate_output_variant",
    ["low_confidence_block", "jurisdiction_conflict"],
)
def test_tier3_outranks_the_later_gate_tiers(
    observation, gate_output, reasoner_context, gate_output_variant
):
    obs = _with_feature(observation, reasoner_context, _NOVEL_FEATURE, True)
    output = (
        _verdict(
            gate_output,
            confidence=LOW_CONFIDENCE_THRESHOLD - 0.1,
            permitted_actions=[DecisionAction.BLOCK],
        )
        if gate_output_variant == "low_confidence_block"
        else _evidence(gate_output, "REGION_A", "REGION_B")
    )
    decision = _resolve(obs, output)
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier3_novel_entity"


# ---------------------------------------------------------------------------
# Parity with the single-pass resolver the split replaced
# ---------------------------------------------------------------------------

_SEVERITY = [
    DecisionAction.ALLOW,
    DecisionAction.CHALLENGE,
    DecisionAction.HOLD,
    DecisionAction.BLOCK,
]


def _single_pass_reference(
    verdict, *, jurisdictions: set[str], novel: bool, registration
) -> tuple[DecisionAction, str | None]:
    """Transcription of the pre-DR-28 resolver: tiers 1-6 in one pass."""
    if verdict is None:
        return DecisionAction.HOLD, "tier1_schema_failure"
    if ADVERSARIAL_CONTROL in verdict.required_controls:
        return DecisionAction.BLOCK, "tier2_adversarial_probe"
    if novel:
        return DecisionAction.HOLD, "tier3_novel_entity"
    conservative = (
        max(verdict.permitted_actions, key=_SEVERITY.index)
        if verdict.permitted_actions
        else DecisionAction.HOLD
    )
    if verdict.confidence < LOW_CONFIDENCE_THRESHOLD and conservative in (
        DecisionAction.HOLD,
        DecisionAction.BLOCK,
    ):
        return DecisionAction.HOLD, "tier4_low_confidence"
    reason = (verdict.escalation_reason or "").lower()
    groups = {g for g in registration.jurisdiction_conflict_groups if jurisdictions & g}
    if (
        JURISDICTION_CONFLICT_CTL in verdict.required_controls
        or (
            verdict.escalate_to_human
            and ("jurisdiction" in reason or "conflict" in reason)
        )
        or len(groups) >= 2
    ):
        return DecisionAction.HOLD, "tier5_jurisdiction_conflict"
    return conservative, None


_VERDICT_SHAPES = {
    "schema_failure": None,
    "adversarial": {"required_controls": [ADVERSARIAL_CONTROL]},
    "low_confidence_block": {
        "confidence": 0.3,
        "permitted_actions": [DecisionAction.BLOCK],
    },
    "low_confidence_allow": {
        "confidence": 0.3,
        "permitted_actions": [DecisionAction.ALLOW],
    },
    "conflict_control": {"required_controls": [JURISDICTION_CONFLICT_CTL]},
    "accept_allow": {"permitted_actions": [DecisionAction.ALLOW]},
    "accept_challenge": {"permitted_actions": [DecisionAction.CHALLENGE]},
    "accept_hold": {"permitted_actions": [DecisionAction.HOLD]},
    "accept_block": {"permitted_actions": [DecisionAction.BLOCK]},
    "accept_empty": {"permitted_actions": []},
}
_VERDICT_SHAPES["conflict_escalation"] = {
    "escalate_to_human": True,
    "escalation_reason": "Regional requirements conflict.",
}
_EVIDENCE_SHAPES = {
    "none": (),
    "one_group": ("REGION_A",),
    "two_groups": ("REGION_A", "REGION_B"),
}


@pytest.mark.parametrize(
    ("verdict_shape", "evidence_shape", "novel"),
    list(itertools.product(_VERDICT_SHAPES, _EVIDENCE_SHAPES, [False, True])),
)
def test_split_matches_the_single_pass_resolver_for_a_retrieving_llm_gate(
    observation, gate_output, reasoner_context, verdict_shape, evidence_shape, novel
):
    # For the gate the split was built around (non-deterministic, retrieving)
    # every combination keeps its action and credited rule, except where the
    # DR-28 evidence rule applies: with no jurisdiction-bearing evidence
    # (empty retrieval) the jurisdiction tier does not run, so a gate that
    # flags a conflict anyway gets its default action instead of a Tier 5
    # HOLD.
    updates = _VERDICT_SHAPES[verdict_shape]
    jurisdictions = _EVIDENCE_SHAPES[evidence_shape]
    output = _evidence(gate_output, *jurisdictions)
    output = (
        output.model_copy(update={"verdict": None})
        if updates is None
        else _verdict(output, **updates)
    )
    obs = _with_feature(observation, reasoner_context, _NOVEL_FEATURE, novel)

    decision = _resolve(obs, output)
    expected = _single_pass_reference(
        output.verdict,
        jurisdictions=set(jurisdictions),
        novel=novel,
        registration=_REG,
    )

    evidence_rule_applies = (
        not novel
        and evidence_shape == "none"
        and verdict_shape in ("conflict_control", "conflict_escalation")
    )
    if evidence_rule_applies:
        assert expected == (DecisionAction.HOLD, "tier5_jurisdiction_conflict")
        assert (decision.decision_action, decision.enforcement_rule_applied) == (
            DecisionAction.ALLOW,
            None,
        )
        assert (
            "tier5_jurisdiction_conflict: skipped — gate evidence carries no "
            "jurisdictions" in decision.override_log
        )
    else:
        assert (decision.decision_action, decision.enforcement_rule_applied) == expected


# ---------------------------------------------------------------------------
# Gate-level resolution on its own
# ---------------------------------------------------------------------------


def test_resolve_gate_output_applies_only_the_gate_level_tiers(gate_output):
    decision = resolve_gate_output(
        gate_output, registration=_REG, decision_id=_DECISION_ID
    )
    assert decision.decision_action == DecisionAction.ALLOW
    assert decision.override_log == [
        "tier1_schema_failure: not fired — gate output present",
        "tier2_adversarial_probe: not fired",
        "tier4_low_confidence: not fired — confidence=0.92",
        "tier5_jurisdiction_conflict: not fired",
        "tier6_default: accepted gate output — action=ALLOW",
    ]


# ---------------------------------------------------------------------------
# Override log completeness
# ---------------------------------------------------------------------------


def test_override_log_records_every_tier_on_a_clean_pass(observation, gate_output):
    decision = _resolve(_gate_route(observation), gate_output)
    assert decision.override_log == [
        "tier1_schema_failure: not fired — gate output present",
        "tier2_adversarial_probe: not fired",
        "tier4_low_confidence: not fired — confidence=0.92",
        "tier5_jurisdiction_conflict: not fired",
        "tier6_default: accepted gate output — action=ALLOW",
        "tier3_novel_entity: not fired — history sufficient",
    ]


def test_override_log_stops_the_gate_tiers_at_the_first_that_fires(
    observation, gate_output
):
    # Tier 2 fires → gate tiers 4-6 do not appear; the decision-level tier
    # is still evaluated once.
    adversarial = _verdict(gate_output, required_controls=[ADVERSARIAL_CONTROL])
    decision = _resolve(_gate_route(observation), adversarial)
    log_text = " ".join(decision.override_log)
    assert "tier2_adversarial_probe: fired" in log_text
    assert "tier4" not in log_text
    assert "tier5" not in log_text
    assert "tier6" not in log_text
    assert decision.override_log[-1] == (
        "tier3_novel_entity: not fired — history sufficient"
    )


def test_resolution_is_deterministic_for_identical_inputs(
    observation, gate_output, reasoner_context
):
    obs = _with_feature(observation, reasoner_context, _NOVEL_FEATURE, True)
    first = _resolve(obs, _evidence(gate_output, "REGION_A", "REGION_B"))
    second = _resolve(obs, _evidence(gate_output, "REGION_A", "REGION_B"))
    assert first == second
