"""Behavioral tests for the ATO Reasoner registration record.

Key contracts:
- ATO_REGISTRATION pins reasoner_id = "ato-reasoner".
- The ato-v1 prompt template is in the allow-list.
- The expected jurisdictions match the corpus loader's tags.
"""

from __future__ import annotations

from reasoner.account_takeover.registry import ATO_REGISTRATION


def test_ato_registration_has_correct_reasoner_id():
    assert ATO_REGISTRATION.reasoner_id == "ato-reasoner"


def test_ato_registration_allows_ato_v1_template():
    assert "ato-v1" in ATO_REGISTRATION.allowed_prompt_template_ids


def test_ato_registration_allows_expected_jurisdictions():
    expected = {"US_FEDERAL", "US_STATE", "EU_GDPR", "INTERNAL"}
    assert expected == ATO_REGISTRATION.allowed_jurisdictions
