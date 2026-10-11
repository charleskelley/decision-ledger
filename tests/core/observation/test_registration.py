"""Behavioral tests for ReasonerRegistration, ReasonerRegistry, and the
registry-extended validate_observation() contracts.

Key contracts:
- ReasonerRegistration is immutable and carries the allowed resource sets.
- validate_observation() without registry: shape checks only (existing behavior).
- validate_observation() with registry: also checks reasoner_id is registered,
  template_id is allowed, and jurisdictions are allowed.
- UnregisteredReasonerError is raised (not ObservationContractError) when the
  reasoner_id is unknown — these are distinct failure modes.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.exceptions import ObservationContractError, UnregisteredReasonerError
from core.observation import (
    ReasonerRegistration,
    ReasonerRegistry,
    validate_observation,
)

# ---------------------------------------------------------------------------
# Helpers — minimal registry that satisfies the ReasonerRegistry protocol
# ---------------------------------------------------------------------------


class _RegistryStub:
    """Minimal ReasonerRegistry for testing — not the StaticReasonerRegistry."""

    def __init__(self, registrations: list[ReasonerRegistration]) -> None:
        self._store = {r.reasoner_id: r for r in registrations}

    def get(self, reasoner_id: str) -> ReasonerRegistration:
        """Return registration or raise."""
        try:
            return self._store[reasoner_id]
        except KeyError:
            raise UnregisteredReasonerError(reasoner_id) from None

    def is_registered(self, reasoner_id: str) -> bool:
        """Return True if registered."""
        return reasoner_id in self._store


def _make_registration(
    *,
    reasoner_id: str = "stub-reasoner",
    gate_ids: frozenset[str] = frozenset({"policy"}),
    template_ids: frozenset[str] = frozenset({"stub-v1"}),
    jurisdictions: frozenset[str] = frozenset({"REGION_A", "REGION_B"}),
) -> ReasonerRegistration:
    return ReasonerRegistration(
        reasoner_id=reasoner_id,
        reasoner_name="Test Reasoner",
        allowed_gate_ids=gate_ids,
        allowed_prompt_template_ids=template_ids,
        allowed_jurisdictions=jurisdictions,
    )


# ---------------------------------------------------------------------------
# ReasonerRegistration model
# ---------------------------------------------------------------------------


def test_reasoner_registration_construction():
    reg = _make_registration()
    assert reg.reasoner_id == "stub-reasoner"
    assert "policy" in reg.allowed_gate_ids
    assert "stub-v1" in reg.allowed_prompt_template_ids
    assert "REGION_A" in reg.allowed_jurisdictions


def test_reasoner_registration_is_immutable():
    reg = _make_registration()
    with pytest.raises(ValidationError):
        reg.reasoner_id = "tampered"


def test_reasoner_registration_allowed_sets_are_frozensets():
    reg = _make_registration()
    assert isinstance(reg.allowed_gate_ids, frozenset)
    assert isinstance(reg.allowed_prompt_template_ids, frozenset)
    assert isinstance(reg.allowed_jurisdictions, frozenset)


# ---------------------------------------------------------------------------
# ReasonerRegistration — enforcement profile (Tier 3 / Tier 5 inputs)
# ---------------------------------------------------------------------------


def test_registration_enforcement_profile_defaults_to_disabled():
    # A registration that says nothing about enforcement disables the
    # structural checks: no novel-entity feature, no conflict groups.
    reg = _make_registration()
    assert reg.novel_entity_feature is None
    assert reg.jurisdiction_conflict_groups == ()


def test_registration_accepts_novel_entity_feature_name():
    reg = ReasonerRegistration(
        reasoner_id="r",
        reasoner_name="R",
        allowed_gate_ids=frozenset({"policy"}),
        allowed_prompt_template_ids=frozenset(),
        allowed_jurisdictions=frozenset(),
        novel_entity_feature="thin_history",
    )
    assert reg.novel_entity_feature == "thin_history"


def test_registration_accepts_jurisdiction_conflict_groups():
    groups = (frozenset({"REGION_A"}), frozenset({"REGION_B", "REGION_C"}))
    reg = ReasonerRegistration(
        reasoner_id="r",
        reasoner_name="R",
        allowed_gate_ids=frozenset({"policy"}),
        allowed_prompt_template_ids=frozenset(),
        allowed_jurisdictions=frozenset(),
        jurisdiction_conflict_groups=groups,
    )
    assert reg.jurisdiction_conflict_groups == groups
    assert all(isinstance(g, frozenset) for g in reg.jurisdiction_conflict_groups)


def test_registration_rejects_non_frozenset_conflict_groups_in_strict_mode():
    # strict=True: a plain set or list is not coerced into a frozenset.
    with pytest.raises(ValidationError):
        ReasonerRegistration(
            reasoner_id="r",
            reasoner_name="R",
            allowed_gate_ids=frozenset({"policy"}),
            allowed_prompt_template_ids=frozenset(),
            allowed_jurisdictions=frozenset(),
            jurisdiction_conflict_groups=({"REGION_A"},),  # type: ignore[arg-type]
        )


def test_registration_enforcement_profile_is_immutable():
    reg = _make_registration()
    with pytest.raises(ValidationError):
        reg.novel_entity_feature = "tampered"
    with pytest.raises(ValidationError):
        reg.jurisdiction_conflict_groups = (frozenset({"X"}),)


# ---------------------------------------------------------------------------
# ReasonerRegistry protocol conformance
# ---------------------------------------------------------------------------


def test_registry_stub_satisfies_protocol():
    stub = _RegistryStub([_make_registration()])
    assert isinstance(stub, ReasonerRegistry)


def test_registry_stub_is_registered_returns_true_for_known_id():
    stub = _RegistryStub([_make_registration()])
    assert stub.is_registered("stub-reasoner")


def test_registry_stub_is_registered_returns_false_for_unknown_id():
    stub = _RegistryStub([_make_registration()])
    assert not stub.is_registered("unknown-reasoner")


def test_registry_stub_get_raises_for_unknown_id():
    stub = _RegistryStub([_make_registration()])
    with pytest.raises(UnregisteredReasonerError) as exc_info:
        stub.get("unknown-reasoner")
    assert exc_info.value.reasoner_id == "unknown-reasoner"


# ---------------------------------------------------------------------------
# validate_observation — without registry (shape checks only)
# ---------------------------------------------------------------------------


def test_validate_observation_passes_without_registry(observation):
    # Existing shape-only behavior must be unchanged.
    validate_observation(observation)


def test_validate_observation_raises_for_missing_reasoner_context(observation):
    obs = observation.model_copy(update={"reasoner_context": None})
    with pytest.raises(ObservationContractError):
        validate_observation(obs)


# ---------------------------------------------------------------------------
# validate_observation — with registry, happy path
# ---------------------------------------------------------------------------


def test_validate_observation_passes_with_registered_reasoner(observation):
    registry = _RegistryStub([_make_registration()])
    validate_observation(observation, registry=registry)


def test_validate_observation_passes_when_jurisdictions_empty(
    observation, gate_context
):
    # Empty jurisdictions list means no filter — always permitted.
    ctx = gate_context.model_copy(
        update={"gate_config": {"jurisdictions": [], "risk_tier": "STANDARD"}}
    )
    obs = observation.model_copy(update={"gate_context": ctx})
    registry = _RegistryStub(
        [_make_registration(jurisdictions=frozenset({"REGION_A"}))]
    )
    validate_observation(obs, registry=registry)


def test_validate_observation_passes_when_allowed_jurisdictions_empty(
    observation, gate_context
):
    # Empty allowed_jurisdictions on the registration means no restriction.
    ctx = gate_context.model_copy(
        update={
            "gate_config": {
                "jurisdictions": ["REGION_A", "REGION_C"],
                "risk_tier": "STANDARD",
            }
        }
    )
    obs = observation.model_copy(update={"gate_context": ctx})
    registry = _RegistryStub([_make_registration(jurisdictions=frozenset())])
    validate_observation(obs, registry=registry)


# ---------------------------------------------------------------------------
# validate_observation — with registry, failure paths
# ---------------------------------------------------------------------------


def test_validate_observation_raises_unregistered_reasoner_error(observation):
    registry = _RegistryStub([])  # no registrations
    with pytest.raises(UnregisteredReasonerError) as exc_info:
        validate_observation(observation, registry=registry)
    assert exc_info.value.reasoner_id == "stub-reasoner"


def test_unregistered_error_is_not_observation_contract_error(observation):
    # These are distinct exception types — callers should handle them separately.
    registry = _RegistryStub([])
    with pytest.raises(UnregisteredReasonerError):
        validate_observation(observation, registry=registry)


def test_validate_observation_raises_for_disallowed_template(observation):
    registry = _RegistryStub(
        [_make_registration(template_ids=frozenset({"other-template-v1"}))]
    )
    with pytest.raises(ObservationContractError) as exc_info:
        validate_observation(observation, registry=registry)
    assert "stub-v1" in str(exc_info.value)


def test_validate_observation_raises_for_disallowed_jurisdiction(
    observation, gate_context
):
    ctx = gate_context.model_copy(
        update={
            "gate_config": {
                "jurisdictions": ["REGION_A", "REGION_C"],
                "risk_tier": "STANDARD",
            }
        }
    )
    obs = observation.model_copy(update={"gate_context": ctx})
    registry = _RegistryStub(
        [_make_registration(jurisdictions=frozenset({"REGION_A"}))]
    )
    with pytest.raises(ObservationContractError) as exc_info:
        validate_observation(obs, registry=registry)
    assert "REGION_C" in str(exc_info.value)
