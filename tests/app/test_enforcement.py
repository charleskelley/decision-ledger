"""Behavioral tests for the deterministic enforcement resolver.

Two levels (DR-28, DR-29), tested exhaustively — these are the governance
guarantees:

- **Gate level** (``resolve_gate_output``): every tier trigger path, the
  deterministic-gate skips, the evidence-gated jurisdiction tier, and a
  failed gate's fail-closed Tier 1.
- **Decision level** (``resolve_decision``): the novel-entity tier on gated
  decisions (advisory verdicts held for review; gate Tiers 1-2 outrank it;
  mandatory gates and the proposal untouched), the bypass check on recorded
  decisions (a rule that does not hold fails closed to HOLD), and the
  combinator over the proposal, mandatory gates, the advisory side, and any
  floor.

Most tier tests read the **single-gate view** (``_resolve``): one advisory
gate, no proposal, no mandatory gate — the shape every pre-DR-29 decision
had. It reports the action, the rule the single-gate resolver credited (the
decision-level rule if one fired, else the gate's), and that resolver's log
(gate lines, then the Tier 3 line), so those tests pin the new resolver to
the old behavior. ``test_split_matches_the_single_pass_resolver_*`` pins it
against a transcription of the pre-split single-pass resolver as well.

Fixture baseline: ``gated_submission`` (stub reasoner, gate mode,
``widget_score`` 0.55, history sufficient) and ``gate_output`` (the fake
gate, non-deterministic, verdict ALLOW at confidence 0.92, one ``REGION_A``
evidence reference — so the jurisdiction tier runs and finds no conflict).
Tiers 3 and 5 read their profile from the registration passed in; the
baseline (``_REG``) declares a novel-entity feature and two conflict groups.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest

from app.enforcement.resolver import (
    ADVERSARIAL_CONTROL,
    JURISDICTION_CONFLICT_CTL,
    LOW_CONFIDENCE_THRESHOLD,
    credited_rule,
    resolve_decision,
    resolve_gate_output,
)
from core.actions import DecisionAction
from core.bundle import GateRecord
from core.gate import Compare, EvidenceRef, GateInput, GateOutput, severity
from core.observation import BypassRule, GateBinding, ReasonerRegistration
from core.submission import BypassCitation, evaluate_bypass
from tests._stub_reasoner.registry import HIGH_SCORE_RULE, LOW_SCORE_RULE

ALLOW, CHALLENGE, HOLD, BLOCK = (
    DecisionAction.ALLOW,
    DecisionAction.CHALLENGE,
    DecisionAction.HOLD,
    DecisionAction.BLOCK,
)

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
        gates=(GateBinding(gate_id="fake"),),
        bypass_rules=(LOW_SCORE_RULE, HIGH_SCORE_RULE),
        recordable_actions=frozenset({ALLOW, BLOCK}),
        novel_entity_feature=novel_entity_feature,
        jurisdiction_conflict_groups=jurisdiction_conflict_groups,
    )


#: Baseline registration — full enforcement profile. Shaped like the stub
#: reasoner's, but with a two-member group so the within-group Tier 5 case is
#: testable.
_REG = _registration()


def _with_feature(submission, name: str, value: object):
    rc = submission.reasoner_context
    rc = rc.model_copy(update={"feature_set": {**rc.feature_set, name: value}})
    return submission.model_copy(update={"reasoner_context": rc})


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


def _for_gate(output: GateOutput, gate_id: str) -> GateOutput:
    """The same output, attributed to ``gate_id``."""
    verdict = output.verdict
    return output.model_copy(
        update={
            "gate_id": gate_id,
            "verdict": (
                verdict.model_copy(update={"gate_id": gate_id}) if verdict else None
            ),
        }
    )


def _record(
    output: GateOutput | None,
    *,
    gate_id: str = "fake",
    mandatory: bool = False,
    registration: ReasonerRegistration = _REG,
    failure: str = "raised RuntimeError: boom",
) -> GateRecord:
    """A gate record as ``app/submit.py`` builds it: ran, or failed when ``None``."""
    if output is None:
        return GateRecord(
            gate_id=gate_id,
            mandatory=mandatory,
            status="failed",
            reason=failure,
            input=GateInput(gate_id=gate_id),
            enforcement=resolve_gate_output(
                None,
                registration=registration,
                decision_id=_DECISION_ID,
                failure=failure,
            ),
        )
    output = _for_gate(output, gate_id)
    return GateRecord(
        gate_id=gate_id,
        mandatory=mandatory,
        status="ran",
        input=GateInput(gate_id=gate_id),
        output=output,
        enforcement=resolve_gate_output(
            output, registration=registration, decision_id=_DECISION_ID
        ),
    )


def _skipped(gate_id: str = "fake", *, mandatory: bool = False) -> GateRecord:
    return GateRecord(
        gate_id=gate_id, mandatory=mandatory, status="skipped", reason="skipped"
    )


def _decide(submission, records, registration=_REG, bypass_check=None):
    return resolve_decision(
        submission=submission,
        records=records,
        registration=registration,
        bypass_check=bypass_check,
        decision_id=_DECISION_ID,
    )


def _resolve(submission, gate_output, registration=_REG):
    """The single-gate view: one advisory gate, no proposal, no mandatory gate.

    ``gate_output=None`` is a gate that produced nothing (it failed closed).
    Returns what the DR-28 single-gate resolver returned: the action, the
    credited rule, and the log (gate lines, then the Tier 3 line).
    """
    record = _record(gate_output, registration=registration)
    decision = _decide(submission, [record], registration)
    assert record.enforcement is not None
    assert decision.override_log[-1].startswith("combinator: ")
    return SimpleNamespace(
        decision_action=decision.decision_action,
        enforcement_rule_applied=(
            decision.enforcement_rule_applied
            or record.enforcement.enforcement_rule_applied
        ),
        override_log=[*record.enforcement.override_log, *decision.override_log[:-1]],
    )


def _recorded(submission, *, label_value: float, rule: BypassRule, action):
    """A record citing ``rule`` with ``action``, at ``label_value``."""
    rc = submission.reasoner_context.model_copy(update={"label_value": label_value})
    return submission.model_copy(
        update={
            "mode": "record",
            "proposed_action": action,
            "bypass": BypassCitation(rule_id=rule.rule_id),
            "reasoner_context": rc,
        }
    )


# ---------------------------------------------------------------------------
# Tier 1: Schema failure → HOLD
# ---------------------------------------------------------------------------


def test_tier1_schema_failure_holds_when_verdict_is_none(gated_submission, gate_output):
    decision = _resolve(
        gated_submission, gate_output.model_copy(update={"verdict": None})
    )
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier1_schema_failure"
    assert decision.override_log[0] == (
        "tier1_schema_failure: fired — Gate output failed schema validation — "
        "routed to HOLD for review"
    )


def test_tier1_holds_when_no_gate_output_reaches_enforcement_on_the_gate_route(
    gated_submission,
):
    decision = _resolve(gated_submission, None)
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier1_schema_failure"


def test_tier1_applies_to_a_deterministic_gate_too(gated_submission, gate_output):
    rules_failure = gate_output.model_copy(
        update={"verdict": None, "deterministic": True}
    )
    decision = _resolve(gated_submission, rules_failure)
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier1_schema_failure"


# ---------------------------------------------------------------------------
# Tier 2: Adversarial probe → BLOCK (non-deterministic gates only)
# ---------------------------------------------------------------------------


def test_tier2_adversarial_probe_blocks_when_control_present(
    gated_submission, gate_output
):
    adversarial = _verdict(gate_output, required_controls=[ADVERSARIAL_CONTROL])
    decision = _resolve(gated_submission, adversarial)
    assert decision.decision_action == DecisionAction.BLOCK
    assert decision.enforcement_rule_applied == "tier2_adversarial_probe"


def test_tier2_does_not_fire_without_adversarial_control(gated_submission, gate_output):
    decision = _resolve(gated_submission, gate_output)
    assert decision.enforcement_rule_applied != "tier2_adversarial_probe"
    assert "tier2_adversarial_probe: not fired" in decision.override_log


def test_tier2_is_skipped_for_a_deterministic_gate(gated_submission, gate_output):
    # A rules or lookup gate cannot be prompt-injected; the sentinel is an
    # LLM-gate signal, so a deterministic gate's control list does not BLOCK.
    deterministic = _verdict(
        gate_output, required_controls=[ADVERSARIAL_CONTROL]
    ).model_copy(update={"deterministic": True})
    decision = _resolve(gated_submission, deterministic)
    assert decision.decision_action == DecisionAction.ALLOW
    assert decision.enforcement_rule_applied is None
    assert "tier2_adversarial_probe: skipped — deterministic gate" in (
        decision.override_log
    )


# ---------------------------------------------------------------------------
# Tier 3: Novel entity (decision-level, registration-declared feature) → HOLD
# ---------------------------------------------------------------------------


def test_tier3_holds_when_registered_novel_entity_feature_is_true(
    gated_submission, gate_output
):
    obs = _with_feature(gated_submission, _NOVEL_FEATURE, True)
    decision = _resolve(obs, gate_output)
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier3_novel_entity"


def test_tier3_does_not_fire_when_history_sufficient(gated_submission, gate_output):
    decision = _resolve(gated_submission, gate_output)
    assert decision.enforcement_rule_applied != "tier3_novel_entity"
    assert "tier3_novel_entity: not fired — history sufficient" in (
        decision.override_log
    )


def test_tier3_never_fires_when_registration_has_no_novel_entity_feature(
    gated_submission, gate_output
):
    # The feature is present and truthy, but the registration declares no
    # novel-entity feature, so the tier is disabled for this reasoner.
    obs = _with_feature(gated_submission, _NOVEL_FEATURE, True)
    decision = _resolve(obs, gate_output, _registration(novel_entity_feature=None))
    assert decision.enforcement_rule_applied != "tier3_novel_entity"
    assert decision.decision_action == DecisionAction.ALLOW
    assert (
        "tier3_novel_entity: not fired — no novel-entity feature configured"
        in decision.override_log
    )


def test_tier3_reads_the_feature_name_declared_on_the_registration(
    gated_submission, gate_output
):
    # A reasoner that calls its novelty signal something else still gets Tier 3,
    # keyed on its own feature name.
    obs = _with_feature(gated_submission, "fresh_entity", True)
    custom = _registration(novel_entity_feature="fresh_entity")
    decision = _resolve(obs, gate_output, custom)
    assert decision.enforcement_rule_applied == "tier3_novel_entity"


def test_tier3_ignores_truthy_feature_under_a_different_name(
    gated_submission, gate_output
):
    obs = _with_feature(gated_submission, "fresh_entity", True)
    decision = _resolve(obs, gate_output)
    assert decision.enforcement_rule_applied != "tier3_novel_entity"


def test_tier3_hold_reason_names_the_entity_not_an_account(
    gated_submission, gate_output
):
    # The DR-26 parity wording ("account history") is retired deliberately
    # with the tier split: the subject of a decision is an entity (DR-12).
    obs = _with_feature(gated_submission, _NOVEL_FEATURE, True)
    decision = _resolve(obs, gate_output)
    assert decision.override_log[-1] == _NOVEL_REASON


def test_tier3_is_evaluated_once_per_decision_after_the_gate_tiers(
    gated_submission, gate_output
):
    obs = _with_feature(gated_submission, _NOVEL_FEATURE, True)
    decision = _resolve(obs, gate_output)
    tier3 = [e for e in decision.override_log if e.startswith("tier3_")]
    assert tier3 == [_NOVEL_REASON]
    assert decision.override_log[-1] == _NOVEL_REASON


# ---------------------------------------------------------------------------
# Tier 4: Low confidence + high-risk action → HOLD (non-deterministic only)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", [DecisionAction.BLOCK, DecisionAction.HOLD])
def test_tier4_holds_when_low_confidence_and_high_risk_action(
    gated_submission, gate_output, action
):
    low = _verdict(
        gate_output,
        confidence=LOW_CONFIDENCE_THRESHOLD - 0.01,
        permitted_actions=[action],
    )
    decision = _resolve(gated_submission, low)
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier4_low_confidence"


@pytest.mark.parametrize("action", [DecisionAction.ALLOW, DecisionAction.CHALLENGE])
def test_tier4_does_not_fire_when_low_confidence_but_low_risk_action(
    gated_submission, gate_output, action
):
    low = _verdict(
        gate_output,
        confidence=LOW_CONFIDENCE_THRESHOLD - 0.01,
        permitted_actions=[action],
    )
    decision = _resolve(gated_submission, low)
    assert decision.enforcement_rule_applied != "tier4_low_confidence"
    assert decision.decision_action == action


def test_tier4_does_not_fire_when_confidence_at_threshold(
    gated_submission, gate_output
):
    at_threshold = _verdict(
        gate_output,
        confidence=LOW_CONFIDENCE_THRESHOLD,
        permitted_actions=[DecisionAction.BLOCK],
    )
    decision = _resolve(gated_submission, at_threshold)
    assert decision.enforcement_rule_applied != "tier4_low_confidence"
    assert decision.decision_action == DecisionAction.BLOCK


def test_tier4_is_skipped_for_a_deterministic_gate(gated_submission, gate_output):
    # A deterministic gate's confidence is not a calibrated self-report; its
    # BLOCK stands.
    rules_block = _verdict(
        gate_output, confidence=0.0, permitted_actions=[DecisionAction.BLOCK]
    ).model_copy(update={"deterministic": True})
    decision = _resolve(gated_submission, rules_block)
    assert decision.decision_action == DecisionAction.BLOCK
    assert decision.enforcement_rule_applied is None
    assert "tier4_low_confidence: skipped — deterministic gate" in (
        decision.override_log
    )


# ---------------------------------------------------------------------------
# Tier 5: Jurisdiction conflict (evidence-gated, registration groups) → HOLD
# ---------------------------------------------------------------------------


def test_tier5_holds_when_jurisdiction_conflict_control_present(
    gated_submission, gate_output
):
    conflict = _verdict(gate_output, required_controls=[JURISDICTION_CONFLICT_CTL])
    decision = _resolve(gated_submission, conflict)
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier5_jurisdiction_conflict"


def test_tier5_holds_when_escalation_reason_names_a_conflict(
    gated_submission, gate_output
):
    escalated = _verdict(
        gate_output,
        escalate_to_human=True,
        escalation_reason="Conflict between regional requirements.",
    )
    decision = _resolve(gated_submission, escalated)
    assert decision.enforcement_rule_applied == "tier5_jurisdiction_conflict"


def test_tier5_control_sentinel_fires_even_with_empty_conflict_groups(
    gated_submission, gate_output
):
    # The gate's explicit signal does not depend on the structural profile.
    conflict = _verdict(gate_output, required_controls=[JURISDICTION_CONFLICT_CTL])
    no_groups = _registration(jurisdiction_conflict_groups=())
    decision = _resolve(gated_submission, conflict, no_groups)
    assert decision.enforcement_rule_applied == "tier5_jurisdiction_conflict"


def test_tier5_holds_when_evidence_spans_two_conflict_groups(
    gated_submission, gate_output
):
    output = _evidence(gate_output, "REGION_A", "REGION_B")
    decision = _resolve(gated_submission, output)
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier5_jurisdiction_conflict"


def test_tier5_reads_jurisdictions_from_every_reference(gated_submission, gate_output):
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
    decision = _resolve(gated_submission, output)
    assert decision.enforcement_rule_applied == "tier5_jurisdiction_conflict"


def test_tier5_does_not_fire_when_evidence_touches_one_group(
    gated_submission, gate_output
):
    # Two jurisdictions, but both inside _GROUP_A — not a conflict.
    output = _evidence(gate_output, "REGION_A", "REGION_A_COAST")
    decision = _resolve(gated_submission, output)
    assert decision.enforcement_rule_applied != "tier5_jurisdiction_conflict"
    assert "tier5_jurisdiction_conflict: not fired" in decision.override_log


def test_tier5_structural_check_never_fires_with_empty_conflict_groups(
    gated_submission, gate_output
):
    output = _evidence(gate_output, "REGION_A", "REGION_B")
    no_groups = _registration(jurisdiction_conflict_groups=())
    decision = _resolve(gated_submission, output, no_groups)
    assert decision.enforcement_rule_applied != "tier5_jurisdiction_conflict"
    assert decision.decision_action == DecisionAction.ALLOW


def test_tier5_ignores_jurisdictions_outside_every_group(gated_submission, gate_output):
    # INTERNAL belongs to no group; REGION_B touches exactly one → no conflict.
    output = _evidence(gate_output, "INTERNAL", "REGION_B")
    decision = _resolve(gated_submission, output)
    assert decision.enforcement_rule_applied != "tier5_jurisdiction_conflict"


def test_tier5_uses_the_groups_declared_on_the_registration(
    gated_submission, gate_output
):
    # A reasoner with its own regional split gets conflict detection on its terms.
    regional = _registration(
        jurisdiction_conflict_groups=(frozenset({"ZONE_X"}), frozenset({"ZONE_Y"}))
    )
    output = _evidence(gate_output, "ZONE_X", "ZONE_Y")
    decision = _resolve(gated_submission, output, regional)
    assert decision.enforcement_rule_applied == "tier5_jurisdiction_conflict"


def test_tier5_treats_duplicate_groups_as_one(gated_submission, gate_output):
    # The same group listed twice is still one group — no conflict.
    dup = _registration(jurisdiction_conflict_groups=(_GROUP_A, _GROUP_A))
    output = _evidence(gate_output, "REGION_A", "REGION_A_COAST")
    decision = _resolve(gated_submission, output, dup)
    assert decision.enforcement_rule_applied != "tier5_jurisdiction_conflict"


def test_tier5_is_skipped_when_evidence_carries_no_jurisdictions(
    gated_submission, gate_output
):
    # DR-28: the tier runs only when the gate's evidence carries jurisdictions
    # — a gate that deals in none (rules, lookups) cannot conflict across them,
    # even if its control list happens to carry the sentinel.
    output = _verdict(
        gate_output, required_controls=[JURISDICTION_CONFLICT_CTL]
    ).model_copy(update={"evidence": (EvidenceRef(source_id="ruleset@1"),)})
    decision = _resolve(gated_submission, output)
    assert decision.enforcement_rule_applied != "tier5_jurisdiction_conflict"
    assert decision.decision_action == DecisionAction.ALLOW
    assert (
        "tier5_jurisdiction_conflict: skipped — gate evidence carries no "
        "jurisdictions" in decision.override_log
    )


def test_tier5_runs_for_a_deterministic_gate_whose_evidence_has_jurisdictions(
    gated_submission, gate_output
):
    output = _evidence(gate_output, "REGION_A", "REGION_B").model_copy(
        update={"deterministic": True}
    )
    decision = _resolve(gated_submission, output)
    assert decision.enforcement_rule_applied == "tier5_jurisdiction_conflict"


# ---------------------------------------------------------------------------
# Tier 6: Default — accept gate output
# ---------------------------------------------------------------------------


def test_tier6_accepts_allow_from_gate(gated_submission, gate_output):
    decision = _resolve(gated_submission, gate_output)
    assert decision.decision_action == DecisionAction.ALLOW
    assert decision.enforcement_rule_applied is None


def test_tier6_selects_most_conservative_from_multiple_permitted_actions(
    gated_submission, gate_output
):
    multi = _verdict(
        gate_output,
        permitted_actions=[DecisionAction.ALLOW, DecisionAction.CHALLENGE],
    )
    decision = _resolve(gated_submission, multi)
    assert decision.decision_action == DecisionAction.CHALLENGE


def test_tier6_passes_hold_through_without_override_rule(gated_submission, gate_output):
    # Gate-accepted HOLD: the gate itself recommended HOLD; no override rule fires.
    # Realized outcome will come from the resolution attempt log post-decision.
    hold = _verdict(gate_output, permitted_actions=[DecisionAction.HOLD])
    decision = _resolve(gated_submission, hold)
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied is None  # gate accepted, not overridden


def test_tier6_empty_permitted_actions_defaults_to_hold(gated_submission, gate_output):
    empty = _verdict(gate_output, permitted_actions=[])
    decision = _resolve(gated_submission, empty)
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
    gated_submission, gate_output, gate_action
):
    # DR-6: a novel entity routes to human review, whatever the gate would
    # have let through or blocked — a confident BLOCK included.
    obs = _with_feature(gated_submission, _NOVEL_FEATURE, True)
    decision = _resolve(obs, _verdict(gate_output, permitted_actions=[gate_action]))
    assert decision.decision_action == DecisionAction.HOLD
    assert decision.enforcement_rule_applied == "tier3_novel_entity"
    assert decision.override_log[-1] == _NOVEL_REASON


def test_tier2_block_outranks_tier3(gated_submission, gate_output):
    obs = _with_feature(gated_submission, _NOVEL_FEATURE, True)
    adversarial = _verdict(gate_output, required_controls=[ADVERSARIAL_CONTROL])
    decision = _resolve(obs, adversarial)
    assert decision.decision_action == DecisionAction.BLOCK
    assert decision.enforcement_rule_applied == "tier2_adversarial_probe"
    assert decision.override_log[-1] == (
        f"{_NOVEL_REASON}; tier2_adversarial_probe outranks it"
    )


def test_tier1_outranks_tier3(gated_submission, gate_output):
    obs = _with_feature(gated_submission, _NOVEL_FEATURE, True)
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
    gated_submission, gate_output, gate_output_variant
):
    obs = _with_feature(gated_submission, _NOVEL_FEATURE, True)
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
    gated_submission, gate_output, verdict_shape, evidence_shape, novel
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
    obs = _with_feature(gated_submission, _NOVEL_FEATURE, novel)

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
# Recorded decisions — the bypass check, no Tier 3
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label_value", "rule", "action"),
    [(0.15, LOW_SCORE_RULE, ALLOW), (0.97, HIGH_SCORE_RULE, BLOCK)],
)
def test_a_record_with_no_mandatory_gate_yields_its_recorded_action(
    gated_submission, label_value, rule, action
):
    record = _recorded(
        gated_submission, label_value=label_value, rule=rule, action=action
    )
    check = evaluate_bypass(rule, record.reasoner_context)
    decision = _decide(record, [_skipped()], bypass_check=check)
    assert decision.decision_action == action
    assert decision.enforcement_rule_applied is None
    assert decision.override_log == [
        f"bypass_rule: {rule.rule_id}@1 holds — observed label_value={label_value!r}",
        "tier3_novel_entity: skipped — recorded decision (the bypass rule is its "
        "justification)",
        f"combinator: most_restrictive(proposed={action.value}) → {action.value}",
    ]


def test_a_record_skips_tier3_even_for_a_novel_entity(gated_submission):
    # A recorded decision's justification is its registered, replayable
    # bypass rule; a rule that should exclude novel entities says so.
    novel = _with_feature(gated_submission, _NOVEL_FEATURE, True)
    record = _recorded(novel, label_value=0.1, rule=LOW_SCORE_RULE, action=ALLOW)
    check = evaluate_bypass(LOW_SCORE_RULE, record.reasoner_context)
    decision = _decide(record, [_skipped()], bypass_check=check)
    assert decision.decision_action == ALLOW
    assert decision.enforcement_rule_applied is None


def test_a_record_whose_rule_does_not_hold_fails_closed_to_hold(gated_submission):
    record = _recorded(
        gated_submission, label_value=0.5, rule=LOW_SCORE_RULE, action=ALLOW
    )
    check = evaluate_bypass(LOW_SCORE_RULE, record.reasoner_context)
    assert check.holds is False
    decision = _decide(record, [_skipped()], bypass_check=check)
    assert decision.decision_action == HOLD
    assert decision.enforcement_rule_applied == "bypass_rule_not_satisfied"
    assert decision.override_log[0] == (
        "bypass_rule_not_satisfied: fired — the cited rule low-score@1 does not "
        "hold (observed label_value=0.5) — HOLD for review"
    )
    assert decision.override_log[-1] == (
        "combinator: most_restrictive(proposed=ALLOW, bypass_floor=HOLD) → HOLD"
    )


def test_a_failed_bypass_cannot_lower_a_recorded_block(gated_submission):
    record = _recorded(
        gated_submission, label_value=0.5, rule=HIGH_SCORE_RULE, action=BLOCK
    )
    check = evaluate_bypass(HIGH_SCORE_RULE, record.reasoner_context)
    decision = _decide(record, [], bypass_check=check)
    assert decision.decision_action == BLOCK
    assert decision.enforcement_rule_applied == "bypass_rule_not_satisfied"


def test_a_bypass_rule_that_cannot_evaluate_fails_closed(gated_submission):
    rule = BypassRule(
        rule_id="needs-a-feature",
        version="1",
        description="",
        predicate=Compare(field="feature_set.absent", op="eq", value=True),
        action=ALLOW,
    )
    record = _recorded(gated_submission, label_value=0.1, rule=rule, action=ALLOW)
    check = evaluate_bypass(rule, record.reasoner_context)
    decision = _decide(record, [], bypass_check=check)
    assert decision.decision_action == HOLD
    assert "feature_set has no key 'absent'" in decision.override_log[0]


def test_a_record_is_never_enforced_without_its_bypass_check(submission):
    with pytest.raises(ValueError, match="with its bypass check"):
        _decide(submission, [], bypass_check=None)


def test_a_mandatory_gate_raises_a_recorded_allow(submission, gate_output):
    hold = _verdict(gate_output, permitted_actions=[HOLD]).model_copy(
        update={"deterministic": True}
    )
    check = evaluate_bypass(LOW_SCORE_RULE, submission.reasoner_context)
    decision = _decide(
        submission,
        [_record(hold, gate_id="lookup", mandatory=True), _skipped()],
        bypass_check=check,
    )
    assert decision.decision_action == HOLD
    assert decision.override_log[-1] == (
        "combinator: most_restrictive(proposed=ALLOW, mandatory:lookup=HOLD) → HOLD"
    )


def test_a_mandatory_gate_cannot_lower_a_recorded_block(gated_submission, gate_output):
    record = _recorded(
        gated_submission, label_value=0.97, rule=HIGH_SCORE_RULE, action=BLOCK
    )
    check = evaluate_bypass(HIGH_SCORE_RULE, record.reasoner_context)
    decision = _decide(
        record,
        [_record(gate_output, gate_id="lookup", mandatory=True)],
        bypass_check=check,
    )
    assert decision.decision_action == BLOCK


def test_a_failed_mandatory_gate_holds_a_recorded_allow(submission):
    check = evaluate_bypass(LOW_SCORE_RULE, submission.reasoner_context)
    failed = _record(
        None, gate_id="lookup", mandatory=True, failure="timed out after 5 ms (budget)"
    )
    decision = _decide(submission, [failed], bypass_check=check)
    assert decision.decision_action == HOLD
    assert failed.enforcement is not None
    assert failed.enforcement.override_log == [
        "tier1_schema_failure: fired — Gate failed (timed out after 5 ms (budget)) — "
        "routed to HOLD for review"
    ]


# ---------------------------------------------------------------------------
# Gated decisions with a gate set — the combinator and Tier 3
# ---------------------------------------------------------------------------


def test_advisory_gates_combine_to_the_most_restrictive(gated_submission, gate_output):
    challenge = _verdict(gate_output, permitted_actions=[CHALLENGE])
    decision = _decide(
        gated_submission,
        [_record(gate_output, gate_id="a"), _record(challenge, gate_id="b")],
    )
    assert decision.decision_action == CHALLENGE
    assert decision.override_log[-1] == (
        "combinator: most_restrictive(advisory=CHALLENGE) → CHALLENGE"
    )


def test_a_mandatory_block_stands_for_a_novel_entity(gated_submission, gate_output):
    # The Phase 1a open question, answered no: Tier 3 holds the reasoner's
    # side for review; it never waives a mandatory gate's BLOCK. BLOCK
    # absorbs, so Tier 3 is not even applied.
    novel = _with_feature(gated_submission, _NOVEL_FEATURE, True)
    block = _verdict(gate_output, permitted_actions=[BLOCK]).model_copy(
        update={"deterministic": True}
    )
    decision = _decide(
        novel,
        [_record(block, gate_id="lookup", mandatory=True), _record(gate_output)],
    )
    assert decision.decision_action == BLOCK
    assert decision.enforcement_rule_applied is None
    assert decision.override_log[0] == (
        "tier3_novel_entity: not applied — a mandatory gate already blocks the decision"
    )


def test_tier3_holds_an_advisory_block_for_review(gated_submission, gate_output):
    novel = _with_feature(gated_submission, _NOVEL_FEATURE, True)
    block = _verdict(gate_output, permitted_actions=[BLOCK])
    decision = _decide(
        novel,
        [_record(gate_output, gate_id="lookup", mandatory=True), _record(block)],
    )
    assert decision.decision_action == HOLD
    assert decision.enforcement_rule_applied == "tier3_novel_entity"


def test_an_advisory_tier2_outranks_tier3_among_several_advisory_gates(
    gated_submission, gate_output
):
    novel = _with_feature(gated_submission, _NOVEL_FEATURE, True)
    probe = _verdict(gate_output, required_controls=[ADVERSARIAL_CONTROL])
    decision = _decide(
        novel, [_record(gate_output, gate_id="a"), _record(probe, gate_id="b")]
    )
    assert decision.decision_action == BLOCK
    assert decision.enforcement_rule_applied is None
    assert decision.override_log[0] == (
        f"{_NOVEL_REASON}; tier2_adversarial_probe outranks it"
    )


def test_tier3_is_credited_when_it_holds_a_block_beside_a_failed_gate(
    gated_submission, gate_output
):
    novel = _with_feature(gated_submission, _NOVEL_FEATURE, True)
    block = _verdict(gate_output, permitted_actions=[BLOCK])
    decision = _decide(novel, [_record(None, gate_id="a"), _record(block, gate_id="b")])
    assert decision.decision_action == HOLD
    assert decision.enforcement_rule_applied == "tier3_novel_entity"


def test_tier3_is_outranked_when_every_advisory_gate_failed_closed(gated_submission):
    novel = _with_feature(gated_submission, _NOVEL_FEATURE, True)
    decision = _decide(novel, [_record(None, gate_id="a"), _record(None, gate_id="b")])
    assert decision.decision_action == HOLD
    assert decision.enforcement_rule_applied is None
    assert decision.override_log[0] == (
        f"{_NOVEL_REASON}; tier1_schema_failure outranks it"
    )


def test_tier3_holds_a_gated_decision_whose_gates_are_all_mandatory(
    gated_submission, gate_output
):
    # A novel entity the reasoner asked the ledger to gate goes to review
    # even when no advisory gate is bound (DR-6).
    novel = _with_feature(gated_submission, _NOVEL_FEATURE, True)
    decision = _decide(novel, [_record(gate_output, gate_id="lookup", mandatory=True)])
    assert decision.decision_action == HOLD
    assert decision.enforcement_rule_applied == "tier3_novel_entity"


def test_the_proposal_is_a_floor_on_a_gated_decision(gated_submission, gate_output):
    proposed = gated_submission.model_copy(update={"proposed_action": CHALLENGE})
    decision = _decide(proposed, [_record(gate_output)])
    assert decision.decision_action == CHALLENGE
    assert decision.override_log[-1] == (
        "combinator: most_restrictive(proposed=CHALLENGE, advisory=ALLOW) → CHALLENGE"
    )


def test_tier3_never_lowers_the_proposal(gated_submission, gate_output):
    novel = _with_feature(gated_submission, _NOVEL_FEATURE, True)
    proposed = novel.model_copy(update={"proposed_action": BLOCK})
    decision = _decide(proposed, [_skipped()])
    assert decision.decision_action == BLOCK


def test_skipped_gates_contribute_nothing(gated_submission, gate_output):
    proposed = gated_submission.model_copy(update={"proposed_action": BLOCK})
    decision = _decide(proposed, [_skipped("a"), _skipped("b", mandatory=True)])
    assert decision.decision_action == BLOCK
    assert (
        decision.override_log[-1]
        == "combinator: most_restrictive(proposed=BLOCK) → BLOCK"
    )


def test_a_decision_with_nothing_to_combine_raises(gated_submission):
    with pytest.raises(ValueError, match="needs a proposed or an enforced action"):
        _decide(gated_submission, [_skipped()])


# ---------------------------------------------------------------------------
# Competing bypass rules — the reasoner cannot cite the lenient one
# ---------------------------------------------------------------------------

_TRUSTED_DEVICE = BypassRule(
    rule_id="trusted-device",
    version="1",
    description="a known device: allow",
    predicate=Compare(field="feature_set.widget_wobble", op="ge", value=1.0),
    action=ALLOW,
)


def test_a_stricter_rule_that_also_holds_floors_the_record(gated_submission):
    # trusted-device (ALLOW) and high-score (BLOCK) both hold at 0.97 with a
    # wobble of 1.0; citing the lenient one records BLOCK anyway.
    record = _recorded(
        gated_submission, label_value=0.97, rule=_TRUSTED_DEVICE, action=ALLOW
    )
    cited = evaluate_bypass(_TRUSTED_DEVICE, record.reasoner_context)
    competing = [evaluate_bypass(HIGH_SCORE_RULE, record.reasoner_context)]
    decision = resolve_decision(
        submission=record,
        records=[],
        registration=_REG,
        bypass_check=cited,
        competing_checks=competing,
        decision_id=_DECISION_ID,
    )
    assert decision.decision_action == BLOCK
    assert decision.enforcement_rule_applied == "bypass_rule_superseded"
    assert decision.override_log[1] == (
        "bypass_rule_superseded: fired — registered rule high-score@1 also holds "
        "and justifies BLOCK, stricter than the cited ALLOW"
    )


def test_a_competing_rule_that_does_not_hold_changes_nothing(submission):
    cited = evaluate_bypass(LOW_SCORE_RULE, submission.reasoner_context)
    competing = [evaluate_bypass(HIGH_SCORE_RULE, submission.reasoner_context)]
    decision = resolve_decision(
        submission=submission,
        records=[],
        registration=_REG,
        bypass_check=cited,
        competing_checks=competing,
        decision_id=_DECISION_ID,
    )
    assert decision.decision_action == ALLOW
    assert decision.enforcement_rule_applied is None
    assert decision.override_log[1] == (
        "bypass_rule_superseded: not fired — no other registered rule that holds "
        "justifies a stricter action"
    )


def test_when_both_bypass_floors_fire_the_stricter_is_credited(gated_submission):
    # The cited trusted-device rule fails (no wobble) while high-score holds.
    rc = gated_submission.reasoner_context
    no_wobble = rc.model_copy(
        update={
            "label_value": 0.97,
            "feature_set": {**rc.feature_set, "widget_wobble": 0.0},
        }
    )
    record = gated_submission.model_copy(
        update={
            "mode": "record",
            "proposed_action": ALLOW,
            "bypass": BypassCitation(rule_id="trusted-device"),
            "reasoner_context": no_wobble,
        }
    )
    decision = resolve_decision(
        submission=record,
        records=[],
        registration=_REG,
        bypass_check=evaluate_bypass(_TRUSTED_DEVICE, no_wobble),
        competing_checks=[evaluate_bypass(HIGH_SCORE_RULE, no_wobble)],
        decision_id=_DECISION_ID,
    )
    assert decision.decision_action == BLOCK
    assert decision.enforcement_rule_applied == "bypass_rule_superseded"


# ---------------------------------------------------------------------------
# credited_rule — the one-rule summary the single-gate bundle used to store
# ---------------------------------------------------------------------------


def _bundle_for(submission, records, decision, *, check=None, competing=()):
    from app.audit import build_bundle

    return build_bundle(
        decision_id="11111111-1111-1111-1111-111111111111",
        submission=submission,
        idempotency_key=submission.event_id,
        ingestion_timestamp=submission.timestamp,
        gates=records,
        decision=decision,
        bypass_check=check,
        other_bypass_checks=competing,
    )


@pytest.mark.parametrize(
    ("verdict_shape", "evidence_shape", "novel"),
    list(
        itertools.product(
            [
                "schema_failure",
                "adversarial",
                "low_confidence_block",
                "conflict_control",
                "accept_allow",
                "accept_block",
            ],
            ["none", "two_groups"],
            [False, True],
        )
    ),
)
def test_credited_rule_reproduces_the_single_gate_credit(
    gated_submission, gate_output, verdict_shape, evidence_shape, novel
):
    updates = _VERDICT_SHAPES[verdict_shape]
    output = _evidence(gate_output, *_EVIDENCE_SHAPES[evidence_shape])
    output = (
        output.model_copy(update={"verdict": None})
        if updates is None
        else _verdict(output, **updates)
    )
    sub = _with_feature(gated_submission, _NOVEL_FEATURE, novel)
    record = _record(output)
    decision = _decide(sub, [record])
    view = _resolve(sub, output)
    assert credited_rule(_bundle_for(sub, [record], decision)) == (
        view.enforcement_rule_applied
    )


def test_credited_rule_is_none_for_a_plain_record(submission):
    check = evaluate_bypass(LOW_SCORE_RULE, submission.reasoner_context)
    decision = _decide(submission, [_skipped()], bypass_check=check)
    bundle = _bundle_for(submission, [_skipped()], decision, check=check)
    assert credited_rule(bundle) is None


def test_credited_rule_names_the_mandatory_gate_rule_that_set_the_action(
    gated_submission, gate_output
):
    low = _verdict(gate_output, confidence=0.3, permitted_actions=[BLOCK])
    records = [_record(low, gate_id="lk", mandatory=True), _record(gate_output)]
    decision = _decide(gated_submission, records)
    assert decision.decision_action == HOLD
    assert credited_rule(_bundle_for(gated_submission, records, decision)) == (
        "tier4_low_confidence"
    )


# ---------------------------------------------------------------------------
# Combinator properties, exhaustively over small gate sets
# ---------------------------------------------------------------------------


#: Gate outcomes: (label, output or None for a failed gate).
def _outcomes(gate_output: GateOutput) -> dict[str, GateOutput | None]:
    return {
        "failed": None,
        "probe": _verdict(gate_output, required_controls=[ADVERSARIAL_CONTROL]),
        "allow": gate_output,
        "challenge": _verdict(gate_output, permitted_actions=[CHALLENGE]),
        "block": _verdict(gate_output, permitted_actions=[BLOCK]),
        "low_confidence_block": _verdict(
            gate_output, confidence=0.3, permitted_actions=[BLOCK]
        ),
    }


def _gate_sets(gate_output: GateOutput, max_size: int = 3):
    """Every multiset of (outcome, mandatory) up to ``max_size`` gates."""
    kinds = [
        (label, output, mandatory)
        for label, output in _outcomes(gate_output).items()
        for mandatory in (False, True)
    ]
    for size in range(max_size + 1):
        yield from itertools.combinations_with_replacement(kinds, size)


def _records_for(gate_set) -> list[GateRecord]:
    return [
        _record(output, gate_id=f"g{i}", mandatory=mandatory)
        for i, (_, output, mandatory) in enumerate(gate_set)
    ]


_PROPOSALS = (None, CHALLENGE, BLOCK)


def _cases(gate_output):
    for gate_set in _gate_sets(gate_output):
        for proposed in _PROPOSALS:
            for novel in (False, True):
                if proposed is None and not gate_set and not novel:
                    continue  # nothing to combine (intake rules it out)
                yield gate_set, proposed, novel


def test_gate_order_cannot_change_a_gated_decision(gated_submission, gate_output):
    for gate_set, proposed, novel in _cases(gate_output):
        sub = _with_feature(gated_submission, _NOVEL_FEATURE, novel).model_copy(
            update={"proposed_action": proposed}
        )
        records = _records_for(gate_set)
        first = _decide(sub, records)
        for perm in itertools.permutations(records):
            again = _decide(sub, list(perm))
            assert (again.decision_action, again.enforcement_rule_applied) == (
                first.decision_action,
                first.enforcement_rule_applied,
            )


def test_no_gate_or_rule_relaxes_the_proposal_or_a_mandatory_gate(
    gated_submission, gate_output
):
    for gate_set, proposed, novel in _cases(gate_output):
        sub = _with_feature(gated_submission, _NOVEL_FEATURE, novel).model_copy(
            update={"proposed_action": proposed}
        )
        records = _records_for(gate_set)
        final = _decide(sub, records).decision_action
        if proposed is not None:
            assert severity(final) >= severity(proposed)
        for record in records:
            if record.mandatory:
                assert severity(final) >= severity(record.enforced_action)  # type: ignore[arg-type]


def test_adding_a_gate_never_lowers_a_gated_decision(gated_submission, gate_output):
    # Monotone (Tier 3 included): the property that makes the short-circuit
    # safe.
    extra_kinds = [
        (output, mandatory)
        for output in _outcomes(gate_output).values()
        for mandatory in (False, True)
    ]
    for gate_set, proposed, novel in _cases(gate_output):
        if len(gate_set) > 2:
            continue
        sub = _with_feature(gated_submission, _NOVEL_FEATURE, novel).model_copy(
            update={"proposed_action": proposed}
        )
        records = _records_for(gate_set)
        before = _decide(sub, records).decision_action
        for output, mandatory in extra_kinds:
            extra = _record(output, gate_id="extra", mandatory=mandatory)
            after = _decide(sub, [*records, extra]).decision_action
            assert severity(after) >= severity(before)


def test_once_blocked_by_the_proposal_or_a_mandatory_gate_advisory_gates_cannot_matter(
    gated_submission, gate_output
):
    # What the short-circuit relies on: if the proposal and the mandatory
    # gates already make the decision BLOCK, any advisory outcome (or none)
    # leaves it BLOCK.
    for gate_set, proposed, novel in _cases(gate_output):
        sub = _with_feature(gated_submission, _NOVEL_FEATURE, novel).model_copy(
            update={"proposed_action": proposed}
        )
        records = _records_for(gate_set)
        mandatory = [r for r in records if r.mandatory]
        blocked = proposed == BLOCK or any(
            r.enforced_action == BLOCK for r in mandatory
        )
        if not blocked:
            continue
        assert _decide(sub, records).decision_action == BLOCK
        assert _decide(sub, mandatory).decision_action == BLOCK


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


def test_override_log_records_every_tier_on_a_clean_pass(gated_submission, gate_output):
    decision = _resolve(gated_submission, gate_output)
    assert decision.override_log == [
        "tier1_schema_failure: not fired — gate output present",
        "tier2_adversarial_probe: not fired",
        "tier4_low_confidence: not fired — confidence=0.92",
        "tier5_jurisdiction_conflict: not fired",
        "tier6_default: accepted gate output — action=ALLOW",
        "tier3_novel_entity: not fired — history sufficient",
    ]


def test_override_log_stops_the_gate_tiers_at_the_first_that_fires(
    gated_submission, gate_output
):
    # Tier 2 fires → gate tiers 4-6 do not appear; the decision-level tier
    # is still evaluated once.
    adversarial = _verdict(gate_output, required_controls=[ADVERSARIAL_CONTROL])
    decision = _resolve(gated_submission, adversarial)
    log_text = " ".join(decision.override_log)
    assert "tier2_adversarial_probe: fired" in log_text
    assert "tier4" not in log_text
    assert "tier5" not in log_text
    assert "tier6" not in log_text
    assert decision.override_log[-1] == (
        "tier3_novel_entity: not fired — history sufficient"
    )


def test_resolution_is_deterministic_for_identical_inputs(
    gated_submission, gate_output
):
    obs = _with_feature(gated_submission, _NOVEL_FEATURE, True)
    first = _resolve(obs, _evidence(gate_output, "REGION_A", "REGION_B"))
    second = _resolve(obs, _evidence(gate_output, "REGION_A", "REGION_B"))
    assert first == second
