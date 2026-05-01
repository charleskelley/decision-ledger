"""Behavioral tests for the deterministic enforcement resolver.

Tests cover every tier trigger path and key priority interactions. These
are the governance guarantees — exhaustive coverage is intentional.

Fixture baseline: login_event (FAST_PATH_ALLOW, reasoner_context populated),
gate_output (ALLOW, confidence=0.92), no jurisdiction-conflicting snippets.
Each test deviates minimally from the baseline to isolate the trigger under test.
"""

from __future__ import annotations

from app.enforcement.resolver import (
    ADVERSARIAL_CONTROL,
    JURISDICTION_CONFLICT_CTL,
    LOW_CONFIDENCE_THRESHOLD,
    NOVEL_ENTITY_FEATURE,
    resolve,
)
from core.actions import DecisionAction
from core.routes import GateRoute
from core.snippet import RetrievedSnippet

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DECISION_ID = "dec-test-001"


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
    login_event, gate_output
):
    # login_event fixture has route=FAST_PATH_ALLOW
    decision = resolve(login_event, gate_output, snippets=[], decision_id=_DECISION_ID)
    assert decision.decision_action == DecisionAction.ALLOW
    assert decision.enforcement_rule_applied is None
    assert "fast_path_allow" in decision.override_log[0]


def test_fast_path_allow_ignores_gate_output_none(login_event):
    decision = resolve(login_event, None, snippets=[], decision_id=_DECISION_ID)
    assert decision.decision_action == DecisionAction.ALLOW


def test_fast_path_block_returns_block(login_event, gate_output, gate_context):
    obs = login_event.model_copy(
        update={
            "route": GateRoute.FAST_PATH_BLOCK,
            "fast_path_rationale": "risk_score=0.91 > 0.85 → FAST_PATH_BLOCK",
        }
    )
    decision = resolve(obs, gate_output, snippets=[], decision_id=_DECISION_ID)
    assert decision.decision_action == DecisionAction.BLOCK


# ---------------------------------------------------------------------------
# Tier 1: Schema failure → HOLD
# ---------------------------------------------------------------------------


def test_tier1_schema_failure_holds_when_gate_output_none(login_event, gate_context):
    obs = login_event.model_copy(
        update={
            "route": GateRoute.ROUTE_TO_GATE,
            "fast_path_rationale": None,
        }
    )
    decision = resolve(obs, None, snippets=[], decision_id=_DECISION_ID)
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier1_schema_failure"


def test_tier1_schema_failure_records_rule_in_override_log(login_event):
    obs = login_event.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    decision = resolve(obs, None, snippets=[], decision_id=_DECISION_ID)
    assert any("tier1_schema_failure" in entry for entry in decision.override_log)


# ---------------------------------------------------------------------------
# Tier 2: Adversarial probe → BLOCK
# ---------------------------------------------------------------------------


def test_tier2_adversarial_probe_blocks_when_control_present(login_event, gate_output):
    obs = login_event.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    adversarial_gate = gate_output.model_copy(
        update={"required_controls": [ADVERSARIAL_CONTROL]}
    )
    decision = resolve(obs, adversarial_gate, snippets=[], decision_id=_DECISION_ID)
    assert decision.decision_action == DecisionAction.BLOCK
    assert decision.enforcement_rule_applied == "tier2_adversarial_probe"


def test_tier2_does_not_fire_without_adversarial_control(login_event, gate_output):
    obs = login_event.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    decision = resolve(obs, gate_output, snippets=[], decision_id=_DECISION_ID)
    assert decision.enforcement_rule_applied != "tier2_adversarial_probe"


# ---------------------------------------------------------------------------
# Tier 3: Novel entity (sparse history) → HOLD
# ---------------------------------------------------------------------------


def test_tier3_novel_entity_holds_when_sparse_history(
    login_event, gate_output, reasoner_context
):
    sparse_rc = reasoner_context.model_copy(
        update={
            "feature_set": {
                **reasoner_context.feature_set,
                NOVEL_ENTITY_FEATURE: True,
            }
        }
    )
    obs = login_event.model_copy(
        update={
            "route": GateRoute.ROUTE_TO_GATE,
            "fast_path_rationale": None,
            "reasoner_context": sparse_rc,
        }
    )
    decision = resolve(obs, gate_output, snippets=[], decision_id=_DECISION_ID)
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier3_novel_entity"


def test_tier3_does_not_fire_when_history_sufficient(login_event, gate_output):
    obs = login_event.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    decision = resolve(obs, gate_output, snippets=[], decision_id=_DECISION_ID)
    assert decision.enforcement_rule_applied != "tier3_novel_entity"


# ---------------------------------------------------------------------------
# Tier 4: Low confidence + high-risk action → HOLD
# ---------------------------------------------------------------------------


def test_tier4_holds_when_low_confidence_and_block_action(login_event, gate_output):
    obs = login_event.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    low_conf_gate = gate_output.model_copy(
        update={
            "confidence": LOW_CONFIDENCE_THRESHOLD - 0.01,
            "permitted_actions": [DecisionAction.BLOCK],
        }
    )
    decision = resolve(obs, low_conf_gate, snippets=[], decision_id=_DECISION_ID)
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier4_low_confidence"


def test_tier4_holds_when_low_confidence_and_hold_action(login_event, gate_output):
    obs = login_event.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    low_conf_gate = gate_output.model_copy(
        update={
            "confidence": LOW_CONFIDENCE_THRESHOLD - 0.01,
            "permitted_actions": [DecisionAction.HOLD],
        }
    )
    decision = resolve(obs, low_conf_gate, snippets=[], decision_id=_DECISION_ID)
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier4_low_confidence"


def test_tier4_does_not_fire_when_low_confidence_but_allow_action(
    login_event, gate_output
):
    obs = login_event.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    low_conf_allow = gate_output.model_copy(
        update={
            "confidence": LOW_CONFIDENCE_THRESHOLD - 0.01,
            "permitted_actions": [DecisionAction.ALLOW],
        }
    )
    decision = resolve(obs, low_conf_allow, snippets=[], decision_id=_DECISION_ID)
    assert decision.enforcement_rule_applied != "tier4_low_confidence"


def test_tier4_does_not_fire_when_confidence_at_threshold(login_event, gate_output):
    obs = login_event.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    at_threshold = gate_output.model_copy(
        update={
            "confidence": LOW_CONFIDENCE_THRESHOLD,
            "permitted_actions": [DecisionAction.BLOCK],
        }
    )
    decision = resolve(obs, at_threshold, snippets=[], decision_id=_DECISION_ID)
    assert decision.enforcement_rule_applied != "tier4_low_confidence"


# ---------------------------------------------------------------------------
# Tier 5: Jurisdiction conflict → HOLD
# ---------------------------------------------------------------------------


def test_tier5_holds_when_jurisdiction_conflict_control_present(
    login_event, gate_output
):
    obs = login_event.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    conflict_gate = gate_output.model_copy(
        update={"required_controls": [JURISDICTION_CONFLICT_CTL]}
    )
    decision = resolve(obs, conflict_gate, snippets=[], decision_id=_DECISION_ID)
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier5_jurisdiction_conflict"


def test_tier5_holds_when_us_and_eu_snippets_both_present(login_event, gate_output):
    obs = login_event.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    snippets = [_snippet("US_FEDERAL"), _snippet("EU_GDPR")]
    decision = resolve(obs, gate_output, snippets=snippets, decision_id=_DECISION_ID)
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier5_jurisdiction_conflict"


def test_tier5_does_not_fire_when_only_us_snippets(login_event, gate_output):
    obs = login_event.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    snippets = [_snippet("US_FEDERAL"), _snippet("US_STATE")]
    decision = resolve(obs, gate_output, snippets=snippets, decision_id=_DECISION_ID)
    assert decision.enforcement_rule_applied != "tier5_jurisdiction_conflict"


# ---------------------------------------------------------------------------
# Tier 6: Default — accept gate output
# ---------------------------------------------------------------------------


def test_tier6_accepts_allow_from_gate(login_event, gate_output):
    obs = login_event.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    decision = resolve(obs, gate_output, snippets=[], decision_id=_DECISION_ID)
    assert decision.decision_action == DecisionAction.ALLOW
    assert decision.enforcement_rule_applied is None


def test_tier6_selects_most_conservative_from_multiple_permitted_actions(
    login_event, gate_output
):
    obs = login_event.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    multi_gate = gate_output.model_copy(
        update={"permitted_actions": [DecisionAction.ALLOW, DecisionAction.CHALLENGE]}
    )
    decision = resolve(obs, multi_gate, snippets=[], decision_id=_DECISION_ID)
    assert decision.decision_action == DecisionAction.CHALLENGE


def test_tier6_passes_hold_through_without_override_rule(login_event, gate_output):
    # Gate-accepted HOLD: the gate itself recommended HOLD; no override rule fires.
    # Realized outcome will come from the resolution attempt log post-decision.
    obs = login_event.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    hold_gate = gate_output.model_copy(
        update={"permitted_actions": [DecisionAction.HOLD]}
    )
    decision = resolve(obs, hold_gate, snippets=[], decision_id=_DECISION_ID)
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied is None  # gate accepted, not overridden


def test_tier6_empty_permitted_actions_defaults_to_hold(login_event, gate_output):
    obs = login_event.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    empty_gate = gate_output.model_copy(update={"permitted_actions": []})
    decision = resolve(obs, empty_gate, snippets=[], decision_id=_DECISION_ID)
    assert decision.decision_action == DecisionAction.HOLD


# ---------------------------------------------------------------------------
# Override log completeness
# ---------------------------------------------------------------------------


def test_override_log_records_all_tiers_on_clean_pass(login_event, gate_output):
    obs = login_event.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    decision = resolve(obs, gate_output, snippets=[], decision_id=_DECISION_ID)
    log_text = " ".join(decision.override_log)
    assert "tier1" in log_text
    assert "tier2" in log_text
    assert "tier3" in log_text
    assert "tier4" in log_text
    assert "tier5" in log_text
    assert "tier6" in log_text


def test_override_log_stops_at_first_fired_tier(login_event, gate_output):
    # Tier 2 fires → tiers 3-6 should not appear in the log.
    obs = login_event.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    adversarial_gate = gate_output.model_copy(
        update={"required_controls": [ADVERSARIAL_CONTROL]}
    )
    decision = resolve(obs, adversarial_gate, snippets=[], decision_id=_DECISION_ID)
    log_text = " ".join(decision.override_log)
    assert "tier2" in log_text
    assert "tier3" not in log_text
    assert "tier6" not in log_text
