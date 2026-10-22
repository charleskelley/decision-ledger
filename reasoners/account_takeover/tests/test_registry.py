"""Behavioral tests for the ATO Reasoner registration and its policy-gate profile.

Key contracts:
- ATO_REGISTRATION pins reasoner_id = "ato-reasoner" and binds only the
  RAG policy gate, as advisory.
- Its two bypass rules are the scorer's bands (DR-30): ``allow-band``
  (``risk_score < 0.20`` → ALLOW) and ``block-band`` (``> 0.95`` → BLOCK),
  disjoint, so no login can cite one while the other holds; everything
  between the bands asks for the gate.
- The enforcement profile names ``sparse_history`` for Tier 3 and splits
  US vs EU jurisdictions into two conflict groups for Tier 5.
- Which templates and jurisdictions ATO may name in its gate context is the
  policy gate's call (DR-28): the gate derives ATO's profile from what ATO
  ships — the ato-v1, ato-v2, and ato-v3 templates (old versions stay so
  historical bundles still validate) and the four jurisdiction tags of its
  corpus — and every Tier 5 conflict group is drawn from those tags.
"""

from __future__ import annotations

import pytest

from ato_reasoner.plugin import PLUGIN
from ato_reasoner.registry import ALLOW_BAND, ATO_REGISTRATION, BLOCK_BAND
from core.actions import DecisionAction
from core.gate import evaluate_predicate
from core.observation import LabelType, ReasonerContext
from core.submission import matching_bypass_rule
from policy_rag_gate.config import ReasonerProfile


def _context(risk_score: float) -> ReasonerContext:
    return ReasonerContext(
        reasoner_id="ato-reasoner",
        reasoner_name="ATO Reasoner",
        model_version="xgb-test",
        label_type=LabelType.NUMERICAL,
        label_name="risk_score",
        label_value=risk_score,
        feature_set={"sparse_history": True},
    )


def _gate_profile() -> ReasonerProfile:
    """What the policy gate will let ATO submit (as its plugin derives it)."""
    return ReasonerProfile.from_assets(
        prompts_dir=PLUGIN.prompts_dir, corpus_dir=PLUGIN.corpus_dir
    )


def test_ato_registration_has_correct_reasoner_id():
    assert ATO_REGISTRATION.reasoner_id == "ato-reasoner"


def test_ato_registration_binds_only_the_policy_gate_as_advisory():
    [binding] = ATO_REGISTRATION.gates
    assert (binding.gate_id, binding.mandatory) == ("policy", False)


def test_ato_binds_the_policy_gate_to_its_corpus_at_tier_one():
    # DR-34: the corpus is a reference on the binding, loaded as ato-policy.
    from policy_rag_gate.corpus import PolicyBindingConfig

    [binding] = ATO_REGISTRATION.gates
    config = PolicyBindingConfig.model_validate(binding.config)
    [corpus] = config.corpora
    assert (corpus.corpus_id, corpus.provider, corpus.tier) == (
        "ato-policy",
        "reference",
        1,
    )
    assert corpus.min_provenance == "pinned_version"
    assert config.k_per_corpus == 5


def test_ato_registers_the_two_score_bands_as_bypass_rules():
    assert ATO_REGISTRATION.bypass_rules == (ALLOW_BAND, BLOCK_BAND)
    assert (ALLOW_BAND.rule_id, ALLOW_BAND.action) == (
        "allow-band",
        DecisionAction.ALLOW,
    )
    assert (BLOCK_BAND.rule_id, BLOCK_BAND.action) == (
        "block-band",
        DecisionAction.BLOCK,
    )
    assert ATO_REGISTRATION.recordable_actions == {
        DecisionAction.ALLOW,
        DecisionAction.BLOCK,
    }


@pytest.mark.parametrize(
    ("risk_score", "rule_id"),
    [
        (0.0, "allow-band"),
        (0.199, "allow-band"),
        (0.20, None),
        (0.61, None),
        (0.95, None),
        (0.951, "block-band"),
        (1.0, "block-band"),
    ],
)
def test_the_bands_cover_the_score_range_with_the_gate_between(risk_score, rule_id):
    # Strict inequalities: a score exactly at a threshold goes to the gate.
    rule = matching_bypass_rule(ATO_REGISTRATION, _context(risk_score))
    assert (rule.rule_id if rule else None) == rule_id


def test_the_bands_never_overlap():
    for score in (i / 1000 for i in range(1001)):
        context = _context(score)
        assert not (
            evaluate_predicate(ALLOW_BAND.predicate, context)
            and evaluate_predicate(BLOCK_BAND.predicate, context)
        )


def test_the_policy_gate_lets_ato_use_every_shipped_template_version():
    assert _gate_profile().template_ids == {"ato-v1", "ato-v2", "ato-v3", "ato-v4"}


def test_the_policy_gate_lets_ato_claim_exactly_its_corpus_jurisdictions():
    expected = {"US_FEDERAL", "US_STATE", "EU_GDPR", "INTERNAL"}
    assert expected == _gate_profile().jurisdictions


def test_ato_registration_declares_sparse_history_as_novel_entity_feature():
    assert ATO_REGISTRATION.novel_entity_feature == "sparse_history"


def test_ato_registration_splits_us_and_eu_into_distinct_conflict_groups():
    groups = set(ATO_REGISTRATION.jurisdiction_conflict_groups)
    assert groups == {frozenset({"US_FEDERAL", "US_STATE"}), frozenset({"EU_GDPR"})}


def test_ato_conflict_groups_are_drawn_from_its_corpus_jurisdictions():
    jurisdictions = _gate_profile().jurisdictions
    for group in ATO_REGISTRATION.jurisdiction_conflict_groups:
        assert group <= jurisdictions
