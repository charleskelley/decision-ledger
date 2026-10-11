"""Behavioral tests for the ATO Reasoner registration record.

Key contracts:
- ATO_REGISTRATION pins reasoner_id = "ato-reasoner".
- The ato-v1, ato-v2, and ato-v3 prompt templates are in the allow-list
  (old versions stay allowed so historical bundles still validate).
- The expected jurisdictions match the corpus loader's tags.
- The enforcement profile names ``sparse_history`` for Tier 3 and splits
  US vs EU jurisdictions into two conflict groups for Tier 5.
"""

from __future__ import annotations

from ato_reasoner.registry import ATO_REGISTRATION


def test_ato_registration_has_correct_reasoner_id():
    assert ATO_REGISTRATION.reasoner_id == "ato-reasoner"


def test_ato_registration_allows_every_shipped_template_version():
    expected = {"ato-v1", "ato-v2", "ato-v3"}
    assert ATO_REGISTRATION.allowed_prompt_template_ids == expected


def test_ato_registration_allows_expected_jurisdictions():
    expected = {"US_FEDERAL", "US_STATE", "EU_GDPR", "INTERNAL"}
    assert expected == ATO_REGISTRATION.allowed_jurisdictions


def test_ato_registration_declares_sparse_history_as_novel_entity_feature():
    assert ATO_REGISTRATION.novel_entity_feature == "sparse_history"


def test_ato_registration_splits_us_and_eu_into_distinct_conflict_groups():
    groups = set(ATO_REGISTRATION.jurisdiction_conflict_groups)
    assert groups == {frozenset({"US_FEDERAL", "US_STATE"}), frozenset({"EU_GDPR"})}


def test_ato_conflict_groups_are_subsets_of_allowed_jurisdictions():
    for group in ATO_REGISTRATION.jurisdiction_conflict_groups:
        assert group <= ATO_REGISTRATION.allowed_jurisdictions
