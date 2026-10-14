"""Behavioral tests for the ATO Reasoner registration and its policy-gate profile.

Key contracts:
- ATO_REGISTRATION pins reasoner_id = "ato-reasoner" and routes only to the
  RAG policy gate.
- The enforcement profile names ``sparse_history`` for Tier 3 and splits
  US vs EU jurisdictions into two conflict groups for Tier 5.
- Which templates and jurisdictions ATO may name in its gate context is the
  policy gate's call (DR-28): the gate derives ATO's profile from what ATO
  ships — the ato-v1, ato-v2, and ato-v3 templates (old versions stay so
  historical bundles still validate) and the four jurisdiction tags of its
  corpus — and every Tier 5 conflict group is drawn from those tags.
"""

from __future__ import annotations

from ato_reasoner.plugin import PLUGIN
from ato_reasoner.registry import ATO_REGISTRATION
from policy_rag_gate.config import ReasonerProfile


def _gate_profile() -> ReasonerProfile:
    """What the policy gate will let ATO submit (as its plugin derives it)."""
    return ReasonerProfile.from_assets(
        prompts_dir=PLUGIN.prompts_dir, corpus_dir=PLUGIN.corpus_dir
    )


def test_ato_registration_has_correct_reasoner_id():
    assert ATO_REGISTRATION.reasoner_id == "ato-reasoner"


def test_ato_registration_routes_only_to_the_policy_gate():
    assert frozenset({"policy"}) == ATO_REGISTRATION.allowed_gate_ids


def test_the_policy_gate_lets_ato_use_every_shipped_template_version():
    assert _gate_profile().template_ids == {"ato-v1", "ato-v2", "ato-v3"}


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
