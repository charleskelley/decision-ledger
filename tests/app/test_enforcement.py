"""Behavioral tests for the deterministic enforcement resolver.

Tests cover every tier trigger path and key priority interactions. These
are the governance guarantees — exhaustive coverage is intentional.

Fixture baseline: observation (FAST_PATH_ALLOW, reasoner_context populated),
gate_output (ALLOW, confidence=0.92), no jurisdiction-conflicting snippets.
Each test deviates minimally from the baseline to isolate the trigger under test.

Tiers 3 and 5 read their profile from the ``ReasonerRegistration`` passed to
``resolve()``. The baseline registration (``_REG``) declares a novel-entity
feature and two conflict groups; Tier 3/5 tests vary the profile to show
that the registration — not a framework constant — drives the behavior.
"""

from __future__ import annotations

from app.enforcement.resolver import (
    ADVERSARIAL_CONTROL,
    JURISDICTION_CONFLICT_CTL,
    LOW_CONFIDENCE_THRESHOLD,
    resolve,
)
from core.actions import DecisionAction
from core.observation import ReasonerRegistration
from core.routes import GateRoute
from core.snippet import RetrievedSnippet

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DECISION_ID = "dec-test-001"
_NOVEL_FEATURE = "thin_history"
_GROUP_A = frozenset({"REGION_A", "REGION_A_COAST"})
_GROUP_B = frozenset({"REGION_B"})


def _registration(
    *,
    novel_entity_feature: str | None = _NOVEL_FEATURE,
    jurisdiction_conflict_groups: tuple[frozenset[str], ...] = (_GROUP_A, _GROUP_B),
) -> ReasonerRegistration:
    return ReasonerRegistration(
        reasoner_id="stub-reasoner",
        reasoner_name="Test Reasoner",
        allowed_gate_ids=frozenset({"policy"}),
        allowed_prompt_template_ids=frozenset({"stub-v1"}),
        allowed_jurisdictions=frozenset({"REGION_A", "REGION_A_COAST", "REGION_B"}),
        novel_entity_feature=novel_entity_feature,
        jurisdiction_conflict_groups=jurisdiction_conflict_groups,
    )


#: Baseline registration — full enforcement profile. Shaped like the stub
#: reasoner's, but with a two-member group so the within-group Tier 5 case is
#: testable.
_REG = _registration()


def _snippet(jurisdiction: str) -> RetrievedSnippet:
    return RetrievedSnippet(
        document_id=f"doc-{jurisdiction}",
        title="Test Policy",
        version="1.0",
        jurisdiction=jurisdiction,
        section_path="Overview",
        text="Sample text.",
        relevance_score=0.8,
        retrieval_path="rrf_only",
    )


# ---------------------------------------------------------------------------
# Fast-path short-circuits
# ---------------------------------------------------------------------------


def test_fast_path_allow_returns_allow_without_tier_evaluation(
    observation, gate_output
):
    # observation fixture has route=FAST_PATH_ALLOW
    decision = resolve(
        observation,
        gate_output,
        snippets=[],
        decision_id=_DECISION_ID,
        registration=_REG,
    )
    assert decision.decision_action == DecisionAction.ALLOW
    assert decision.enforcement_rule_applied is None
    assert "fast_path_allow" in decision.override_log[0]


def test_fast_path_allow_ignores_gate_output_none(observation):
    decision = resolve(
        observation, None, snippets=[], decision_id=_DECISION_ID, registration=_REG
    )
    assert decision.decision_action == DecisionAction.ALLOW


def test_fast_path_block_returns_block(observation, gate_output, gate_context):
    obs = observation.model_copy(
        update={
            "route": GateRoute.FAST_PATH_BLOCK,
            "fast_path_rationale": "widget_score=0.97 > 0.95 → FAST_PATH_BLOCK",
        }
    )
    decision = resolve(
        obs, gate_output, snippets=[], decision_id=_DECISION_ID, registration=_REG
    )
    assert decision.decision_action == DecisionAction.BLOCK


# ---------------------------------------------------------------------------
# Tier 1: Schema failure → HOLD
# ---------------------------------------------------------------------------


def test_tier1_schema_failure_holds_when_gate_output_none(observation, gate_context):
    obs = observation.model_copy(
        update={
            "route": GateRoute.ROUTE_TO_GATE,
            "fast_path_rationale": None,
        }
    )
    decision = resolve(
        obs, None, snippets=[], decision_id=_DECISION_ID, registration=_REG
    )
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier1_schema_failure"


def test_tier1_schema_failure_records_rule_in_override_log(observation):
    obs = observation.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    decision = resolve(
        obs, None, snippets=[], decision_id=_DECISION_ID, registration=_REG
    )
    assert any("tier1_schema_failure" in entry for entry in decision.override_log)


# ---------------------------------------------------------------------------
# Tier 2: Adversarial probe → BLOCK
# ---------------------------------------------------------------------------


def test_tier2_adversarial_probe_blocks_when_control_present(observation, gate_output):
    obs = observation.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    adversarial_gate = gate_output.model_copy(
        update={"required_controls": [ADVERSARIAL_CONTROL]}
    )
    decision = resolve(
        obs, adversarial_gate, snippets=[], decision_id=_DECISION_ID, registration=_REG
    )
    assert decision.decision_action == DecisionAction.BLOCK
    assert decision.enforcement_rule_applied == "tier2_adversarial_probe"


def test_tier2_does_not_fire_without_adversarial_control(observation, gate_output):
    obs = observation.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    decision = resolve(
        obs, gate_output, snippets=[], decision_id=_DECISION_ID, registration=_REG
    )
    assert decision.enforcement_rule_applied != "tier2_adversarial_probe"


# ---------------------------------------------------------------------------
# Tier 3: Novel entity (registration-declared feature) → HOLD
# ---------------------------------------------------------------------------


def _gate_route(observation):
    return observation.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )


def _with_feature(observation, reasoner_context, name: str, value: object):
    rc = reasoner_context.model_copy(
        update={"feature_set": {**reasoner_context.feature_set, name: value}}
    )
    return _gate_route(observation).model_copy(update={"reasoner_context": rc})


def test_tier3_holds_when_registered_novel_entity_feature_is_true(
    observation, gate_output, reasoner_context
):
    obs = _with_feature(observation, reasoner_context, _NOVEL_FEATURE, True)
    decision = resolve(
        obs, gate_output, snippets=[], decision_id=_DECISION_ID, registration=_REG
    )
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier3_novel_entity"


def test_tier3_does_not_fire_when_history_sufficient(observation, gate_output):
    obs = _gate_route(observation)
    decision = resolve(
        obs, gate_output, snippets=[], decision_id=_DECISION_ID, registration=_REG
    )
    assert decision.enforcement_rule_applied != "tier3_novel_entity"
    assert any(
        "tier3_novel_entity: not fired — history sufficient" in e
        for e in decision.override_log
    )


def test_tier3_never_fires_when_registration_has_no_novel_entity_feature(
    observation, gate_output, reasoner_context
):
    # The feature is present and truthy, but the registration declares no
    # novel-entity feature, so the tier is disabled for this reasoner.
    obs = _with_feature(observation, reasoner_context, _NOVEL_FEATURE, True)
    no_profile = _registration(novel_entity_feature=None)
    decision = resolve(
        obs, gate_output, snippets=[], decision_id=_DECISION_ID, registration=no_profile
    )
    assert decision.enforcement_rule_applied != "tier3_novel_entity"
    assert decision.decision_action == DecisionAction.ALLOW
    assert any(
        "tier3_novel_entity: not fired — no novel-entity feature configured" in e
        for e in decision.override_log
    )


def test_tier3_reads_the_feature_name_declared_on_the_registration(
    observation, gate_output, reasoner_context
):
    # A reasoner that calls its novelty signal something else still gets Tier 3,
    # keyed on its own feature name.
    obs = _with_feature(observation, reasoner_context, "fresh_entity", True)
    custom = _registration(novel_entity_feature="fresh_entity")
    decision = resolve(
        obs, gate_output, snippets=[], decision_id=_DECISION_ID, registration=custom
    )
    assert decision.enforcement_rule_applied == "tier3_novel_entity"


def test_tier3_ignores_truthy_feature_under_a_different_name(
    observation, gate_output, reasoner_context
):
    obs = _with_feature(observation, reasoner_context, "fresh_entity", True)
    decision = resolve(
        obs, gate_output, snippets=[], decision_id=_DECISION_ID, registration=_REG
    )
    assert decision.enforcement_rule_applied != "tier3_novel_entity"


def test_tier3_hold_reason_text_is_unchanged_by_the_registration_profile(
    observation, gate_output, reasoner_context
):
    # override_log is persisted in every bundle and is the audit trail, so the
    # Tier 3 reason must be byte-identical to what the pre-profile resolver
    # wrote for the same input. Rewording it is an audit-text change that
    # needs its own decision record, not a side effect of the refactor.
    obs = _with_feature(observation, reasoner_context, _NOVEL_FEATURE, True)
    decision = resolve(
        obs, gate_output, snippets=[], decision_id=_DECISION_ID, registration=_REG
    )
    assert decision.override_log[-1] == (
        "tier3_novel_entity: fired — Insufficient account history for confident "
        "decision — HOLD for review"
    )


# ---------------------------------------------------------------------------
# Tier 4: Low confidence + high-risk action → HOLD
# ---------------------------------------------------------------------------


def test_tier4_holds_when_low_confidence_and_block_action(observation, gate_output):
    obs = observation.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    low_conf_gate = gate_output.model_copy(
        update={
            "confidence": LOW_CONFIDENCE_THRESHOLD - 0.01,
            "permitted_actions": [DecisionAction.BLOCK],
        }
    )
    decision = resolve(
        obs, low_conf_gate, snippets=[], decision_id=_DECISION_ID, registration=_REG
    )
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier4_low_confidence"


def test_tier4_holds_when_low_confidence_and_hold_action(observation, gate_output):
    obs = observation.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    low_conf_gate = gate_output.model_copy(
        update={
            "confidence": LOW_CONFIDENCE_THRESHOLD - 0.01,
            "permitted_actions": [DecisionAction.HOLD],
        }
    )
    decision = resolve(
        obs, low_conf_gate, snippets=[], decision_id=_DECISION_ID, registration=_REG
    )
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier4_low_confidence"


def test_tier4_does_not_fire_when_low_confidence_but_allow_action(
    observation, gate_output
):
    obs = observation.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    low_conf_allow = gate_output.model_copy(
        update={
            "confidence": LOW_CONFIDENCE_THRESHOLD - 0.01,
            "permitted_actions": [DecisionAction.ALLOW],
        }
    )
    decision = resolve(
        obs, low_conf_allow, snippets=[], decision_id=_DECISION_ID, registration=_REG
    )
    assert decision.enforcement_rule_applied != "tier4_low_confidence"


def test_tier4_does_not_fire_when_confidence_at_threshold(observation, gate_output):
    obs = observation.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    at_threshold = gate_output.model_copy(
        update={
            "confidence": LOW_CONFIDENCE_THRESHOLD,
            "permitted_actions": [DecisionAction.BLOCK],
        }
    )
    decision = resolve(
        obs, at_threshold, snippets=[], decision_id=_DECISION_ID, registration=_REG
    )
    assert decision.enforcement_rule_applied != "tier4_low_confidence"


# ---------------------------------------------------------------------------
# Tier 5: Jurisdiction conflict (registration-declared groups) → HOLD
# ---------------------------------------------------------------------------


def test_tier5_holds_when_jurisdiction_conflict_control_present(
    observation, gate_output
):
    obs = _gate_route(observation)
    conflict_gate = gate_output.model_copy(
        update={"required_controls": [JURISDICTION_CONFLICT_CTL]}
    )
    decision = resolve(
        obs, conflict_gate, snippets=[], decision_id=_DECISION_ID, registration=_REG
    )
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier5_jurisdiction_conflict"


def test_tier5_control_sentinel_fires_even_with_empty_conflict_groups(
    observation, gate_output
):
    # The gate's explicit signal does not depend on the structural profile.
    obs = _gate_route(observation)
    conflict_gate = gate_output.model_copy(
        update={"required_controls": [JURISDICTION_CONFLICT_CTL]}
    )
    no_groups = _registration(jurisdiction_conflict_groups=())
    decision = resolve(
        obs,
        conflict_gate,
        snippets=[],
        decision_id=_DECISION_ID,
        registration=no_groups,
    )
    assert decision.enforcement_rule_applied == "tier5_jurisdiction_conflict"


def test_tier5_holds_when_snippets_span_two_conflict_groups(observation, gate_output):
    obs = _gate_route(observation)
    snippets = [_snippet("REGION_A"), _snippet("REGION_B")]
    decision = resolve(
        obs, gate_output, snippets=snippets, decision_id=_DECISION_ID, registration=_REG
    )
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier5_jurisdiction_conflict"


def test_tier5_does_not_fire_when_snippets_touch_one_group(observation, gate_output):
    obs = _gate_route(observation)
    # Two jurisdictions, but both inside _GROUP_A — not a conflict.
    snippets = [_snippet("REGION_A"), _snippet("REGION_A_COAST")]
    decision = resolve(
        obs, gate_output, snippets=snippets, decision_id=_DECISION_ID, registration=_REG
    )
    assert decision.enforcement_rule_applied != "tier5_jurisdiction_conflict"
    assert any(
        e == "tier5_jurisdiction_conflict: not fired" for e in decision.override_log
    )


def test_tier5_structural_check_never_fires_with_empty_conflict_groups(
    observation, gate_output
):
    obs = _gate_route(observation)
    snippets = [_snippet("REGION_A"), _snippet("REGION_B")]
    no_groups = _registration(jurisdiction_conflict_groups=())
    decision = resolve(
        obs,
        gate_output,
        snippets=snippets,
        decision_id=_DECISION_ID,
        registration=no_groups,
    )
    assert decision.enforcement_rule_applied != "tier5_jurisdiction_conflict"
    assert decision.decision_action == DecisionAction.ALLOW


def test_tier5_ignores_jurisdictions_outside_every_group(observation, gate_output):
    # INTERNAL belongs to no group; REGION_B touches exactly one → no conflict.
    obs = _gate_route(observation)
    snippets = [_snippet("INTERNAL"), _snippet("REGION_B")]
    decision = resolve(
        obs, gate_output, snippets=snippets, decision_id=_DECISION_ID, registration=_REG
    )
    assert decision.enforcement_rule_applied != "tier5_jurisdiction_conflict"


def test_tier5_uses_the_groups_declared_on_the_registration(observation, gate_output):
    # A reasoner with its own regional split gets conflict detection on its terms.
    obs = _gate_route(observation)
    regional = _registration(
        jurisdiction_conflict_groups=(frozenset({"ZONE_X"}), frozenset({"ZONE_Y"}))
    )
    snippets = [_snippet("ZONE_X"), _snippet("ZONE_Y")]
    decision = resolve(
        obs,
        gate_output,
        snippets=snippets,
        decision_id=_DECISION_ID,
        registration=regional,
    )
    assert decision.enforcement_rule_applied == "tier5_jurisdiction_conflict"


def test_tier5_treats_duplicate_groups_as_one(observation, gate_output):
    # The same group listed twice is still one group — no conflict.
    obs = _gate_route(observation)
    dup = _registration(jurisdiction_conflict_groups=(_GROUP_A, _GROUP_A))
    snippets = [_snippet("REGION_A"), _snippet("REGION_A_COAST")]
    decision = resolve(
        obs, gate_output, snippets=snippets, decision_id=_DECISION_ID, registration=dup
    )
    assert decision.enforcement_rule_applied != "tier5_jurisdiction_conflict"


# ---------------------------------------------------------------------------
# Tier 6: Default — accept gate output
# ---------------------------------------------------------------------------


def test_tier6_accepts_allow_from_gate(observation, gate_output):
    obs = observation.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    decision = resolve(
        obs, gate_output, snippets=[], decision_id=_DECISION_ID, registration=_REG
    )
    assert decision.decision_action == DecisionAction.ALLOW
    assert decision.enforcement_rule_applied is None


def test_tier6_selects_most_conservative_from_multiple_permitted_actions(
    observation, gate_output
):
    obs = observation.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    multi_gate = gate_output.model_copy(
        update={"permitted_actions": [DecisionAction.ALLOW, DecisionAction.CHALLENGE]}
    )
    decision = resolve(
        obs, multi_gate, snippets=[], decision_id=_DECISION_ID, registration=_REG
    )
    assert decision.decision_action == DecisionAction.CHALLENGE


def test_tier6_passes_hold_through_without_override_rule(observation, gate_output):
    # Gate-accepted HOLD: the gate itself recommended HOLD; no override rule fires.
    # Realized outcome will come from the resolution attempt log post-decision.
    obs = observation.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    hold_gate = gate_output.model_copy(
        update={"permitted_actions": [DecisionAction.HOLD]}
    )
    decision = resolve(
        obs, hold_gate, snippets=[], decision_id=_DECISION_ID, registration=_REG
    )
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied is None  # gate accepted, not overridden


def test_tier6_empty_permitted_actions_defaults_to_hold(observation, gate_output):
    obs = observation.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    empty_gate = gate_output.model_copy(update={"permitted_actions": []})
    decision = resolve(
        obs, empty_gate, snippets=[], decision_id=_DECISION_ID, registration=_REG
    )
    assert decision.decision_action == DecisionAction.HOLD


# ---------------------------------------------------------------------------
# Override log completeness
# ---------------------------------------------------------------------------


def test_override_log_records_all_tiers_on_clean_pass(observation, gate_output):
    obs = observation.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    decision = resolve(
        obs, gate_output, snippets=[], decision_id=_DECISION_ID, registration=_REG
    )
    log_text = " ".join(decision.override_log)
    assert "tier1" in log_text
    assert "tier2" in log_text
    assert "tier3" in log_text
    assert "tier4" in log_text
    assert "tier5" in log_text
    assert "tier6" in log_text


def test_override_log_stops_at_first_fired_tier(observation, gate_output):
    # Tier 2 fires → tiers 3-6 should not appear in the log.
    obs = observation.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    adversarial_gate = gate_output.model_copy(
        update={"required_controls": [ADVERSARIAL_CONTROL]}
    )
    decision = resolve(
        obs, adversarial_gate, snippets=[], decision_id=_DECISION_ID, registration=_REG
    )
    log_text = " ".join(decision.override_log)
    assert "tier2" in log_text
    assert "tier3" not in log_text
    assert "tier6" not in log_text
