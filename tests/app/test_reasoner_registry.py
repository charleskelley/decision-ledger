"""Behavioral tests for StaticReasonerRegistry and the ATO registration.

Key contracts:
- StaticReasonerRegistry rejects duplicate reasoner_id at construction.
- get() raises UnregisteredReasonerError for unknown ids.
- ATO_REASONER_REGISTRATION has the expected id, template, and jurisdictions.
- ATO_REGISTRY is a ready-to-use registry containing the ATO registration.
"""

from __future__ import annotations

import pytest

from app.reasoner_registry import (
    ATO_REASONER_REGISTRATION,
    ATO_REGISTRY,
    StaticReasonerRegistry,
)
from core.exceptions import UnregisteredReasonerError
from core.observation import ReasonerRegistration, ReasonerRegistry


def _reg(reasoner_id: str) -> ReasonerRegistration:
    return ReasonerRegistration(
        reasoner_id=reasoner_id,
        reasoner_name=f"Reasoner {reasoner_id}",
        allowed_prompt_template_ids=frozenset({"tmpl-v1"}),
        allowed_jurisdictions=frozenset({"US_FEDERAL"}),
    )


# ---------------------------------------------------------------------------
# StaticReasonerRegistry construction
# ---------------------------------------------------------------------------


def test_static_registry_satisfies_protocol():
    assert isinstance(StaticReasonerRegistry([]), ReasonerRegistry)


def test_static_registry_rejects_duplicate_reasoner_id():
    with pytest.raises(ValueError, match="Duplicate"):
        StaticReasonerRegistry([_reg("dup"), _reg("dup")])


def test_static_registry_accepts_multiple_distinct_registrations():
    registry = StaticReasonerRegistry([_reg("r-1"), _reg("r-2")])
    assert registry.is_registered("r-1")
    assert registry.is_registered("r-2")


# ---------------------------------------------------------------------------
# StaticReasonerRegistry get / is_registered
# ---------------------------------------------------------------------------


def test_is_registered_true_for_known_id():
    registry = StaticReasonerRegistry([_reg("r-1")])
    assert registry.is_registered("r-1") is True


def test_is_registered_false_for_unknown_id():
    registry = StaticReasonerRegistry([_reg("r-1")])
    assert registry.is_registered("unknown") is False


def test_get_returns_registration_for_known_id():
    reg = _reg("r-1")
    registry = StaticReasonerRegistry([reg])
    assert registry.get("r-1") == reg


def test_get_raises_unregistered_error_for_unknown_id():
    registry = StaticReasonerRegistry([])
    with pytest.raises(UnregisteredReasonerError) as exc_info:
        registry.get("unknown")
    assert exc_info.value.reasoner_id == "unknown"


# ---------------------------------------------------------------------------
# ATO registration constants
# ---------------------------------------------------------------------------


def test_ato_registration_has_correct_reasoner_id():
    assert ATO_REASONER_REGISTRATION.reasoner_id == "ato-reasoner"


def test_ato_registration_allows_ato_v1_template():
    assert "ato-v1" in ATO_REASONER_REGISTRATION.allowed_prompt_template_ids


def test_ato_registration_allows_expected_jurisdictions():
    expected = {"US_FEDERAL", "US_STATE", "EU_GDPR", "INTERNAL"}
    assert expected == ATO_REASONER_REGISTRATION.allowed_jurisdictions


def test_ato_registry_is_registered():
    assert ATO_REGISTRY.is_registered("ato-reasoner")


def test_ato_registry_get_returns_ato_registration():
    assert ATO_REGISTRY.get("ato-reasoner") == ATO_REASONER_REGISTRATION
