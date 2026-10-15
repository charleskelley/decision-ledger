"""Behavioral tests for the framework's StaticReasonerRegistry.

Reasoner-specific registration tests live with the reasoner package
(e.g., reasoners/account_takeover/tests/test_registry.py for ATO).

Key contracts:
- StaticReasonerRegistry rejects duplicate reasoner_id at construction.
- get() raises UnregisteredReasonerError for unknown ids.
"""

from __future__ import annotations

import pytest

from app.reasoner_registry import StaticReasonerRegistry
from core.exceptions import UnregisteredReasonerError
from core.observation import GateBinding, ReasonerRegistration, ReasonerRegistry


def _reg(reasoner_id: str) -> ReasonerRegistration:
    return ReasonerRegistration(
        reasoner_id=reasoner_id,
        reasoner_name=f"Reasoner {reasoner_id}",
        gates=(GateBinding(gate_id="policy"),),
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
# The registrations resource view: versions and sources (DR-31)
# ---------------------------------------------------------------------------


def test_entries_carry_each_registrations_version_and_source():
    from app.reasoner_registry import INLINE_SOURCE
    from core.observation import registration_version

    first, second = _reg("stub-reasoner"), _reg("b-reasoner")
    registry = StaticReasonerRegistry(
        [second, first], sources={"b-reasoner": "file:/etc/r.yaml"}
    )

    entries = registry.entries()

    assert [e.reasoner_id for e in entries] == ["b-reasoner", "stub-reasoner"]
    assert entries[0].source == "file:/etc/r.yaml"
    assert entries[1].source == INLINE_SOURCE
    assert entries[1].version == registration_version(first)
    assert registry.entry("stub-reasoner") is entries[1]


def test_an_unknown_entry_raises_unregistered():
    registry = StaticReasonerRegistry([_reg("stub-reasoner")])
    with pytest.raises(UnregisteredReasonerError):
        registry.entry("nobody")
