"""Behavioral tests for ReasonerRegistration, its parts, and ReasonerRegistry.

Key contracts (DR-29, DR-30):
- A registration binds a gate set (``GateBinding``: mandatory or advisory,
  each gate once), registers bypass rules (unique ids, each justifying one
  action), and lists the actions the reasoner may record; every rule's
  action must be recordable. It carries nothing gate-specific (DR-28).
- ``BypassRule`` predicates are the closed predicate language and round-trip
  through JSON with the registration.
- Registrations, bindings, and rules are immutable.
- A registry satisfies ``ReasonerRegistry`` structurally.
- ``registration_version`` content-addresses a registration (DR-31): equal
  registrations have equal versions in every process (sets are canonically
  ordered), and any change to what enforcement or intake reads changes it.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from pydantic import ValidationError

from core.actions import DecisionAction
from core.exceptions import UnregisteredReasonerError
from core.gate import AllOf, Compare, Not
from core.observation import (
    BypassRule,
    GateBinding,
    ReasonerRegistration,
    ReasonerRegistry,
    canonical_registration,
    registration_version,
)

ALLOW, CHALLENGE, HOLD, BLOCK = (
    DecisionAction.ALLOW,
    DecisionAction.CHALLENGE,
    DecisionAction.HOLD,
    DecisionAction.BLOCK,
)


# ---------------------------------------------------------------------------
# Helpers
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


def _rule(
    rule_id: str = "low-score",
    *,
    action: DecisionAction = ALLOW,
    version: str = "1",
) -> BypassRule:
    return BypassRule(
        rule_id=rule_id,
        version=version,
        description="score below 0.2",
        predicate=Compare(field="label_value", op="lt", value=0.2),
        action=action,
    )


def _registration(**overrides: object) -> ReasonerRegistration:
    fields: dict[str, object] = {
        "reasoner_id": "stub-reasoner",
        "reasoner_name": "Test Reasoner",
        "gates": (GateBinding(gate_id="fake"),),
    }
    fields.update(overrides)
    return ReasonerRegistration(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# GateBinding
# ---------------------------------------------------------------------------


def test_a_gate_binding_is_advisory_unless_marked_mandatory():
    assert GateBinding(gate_id="fake").mandatory is False
    assert GateBinding(gate_id="fake", mandatory=True).mandatory is True


def test_a_gate_binding_needs_a_gate_id():
    with pytest.raises(ValidationError):
        GateBinding(gate_id="")


def test_a_gate_binding_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        GateBinding(gate_id="fake", optional=True)  # type: ignore[call-arg]


def test_a_gate_binding_is_immutable():
    binding = GateBinding(gate_id="fake")
    with pytest.raises(ValidationError):
        binding.mandatory = True


# ---------------------------------------------------------------------------
# BypassRule
# ---------------------------------------------------------------------------


def test_a_bypass_rule_ties_a_predicate_to_one_action():
    rule = _rule(action=BLOCK)
    assert rule.action == BLOCK
    assert isinstance(rule.predicate, Compare)


def test_a_bypass_rule_needs_an_id_and_a_version():
    with pytest.raises(ValidationError):
        _rule(rule_id="")
    with pytest.raises(ValidationError):
        _rule(version="")


def test_a_bypass_rule_predicate_must_be_a_predicate_node():
    with pytest.raises(ValidationError):
        BypassRule(
            rule_id="r",
            version="1",
            description="",
            predicate="label_value < 0.2",  # type: ignore[arg-type]
            action=ALLOW,
        )


def test_a_bypass_rule_round_trips_through_json_with_a_nested_predicate():
    rule = BypassRule(
        rule_id="quiet-widget",
        version="2",
        description="low score and not thin history",
        predicate=AllOf(
            predicates=[
                Compare(field="label_value", op="lt", value=0.2),
                Not(
                    predicate=Compare(
                        field="feature_set.thin_history", op="eq", value=True
                    )
                ),
            ]
        ),
        action=ALLOW,
    )
    assert BypassRule.model_validate_json(rule.model_dump_json()) == rule


def test_a_bypass_rule_is_immutable():
    rule = _rule()
    with pytest.raises(ValidationError):
        rule.action = BLOCK


# ---------------------------------------------------------------------------
# ReasonerRegistration
# ---------------------------------------------------------------------------


def test_a_registration_binds_its_gate_set_in_order():
    reg = _registration(
        gates=(
            GateBinding(gate_id="fake"),
            GateBinding(gate_id="fake-lookup", mandatory=True),
        )
    )
    assert [b.gate_id for b in reg.gates] == ["fake", "fake-lookup"]
    assert reg.gate_ids == frozenset({"fake", "fake-lookup"})
    assert reg.binding("fake-lookup") == GateBinding(
        gate_id="fake-lookup", mandatory=True
    )
    assert reg.binding("missing") is None


def test_a_registration_needs_a_gate_set_even_an_empty_one():
    with pytest.raises(ValidationError):
        ReasonerRegistration(reasoner_id="r", reasoner_name="R")  # type: ignore[call-arg]
    assert _registration(gates=()).gate_ids == frozenset()


def test_a_registration_rejects_a_gate_bound_twice():
    with pytest.raises(ValidationError, match="bound more than once"):
        _registration(
            gates=(
                GateBinding(gate_id="fake"),
                GateBinding(gate_id="fake", mandatory=True),
            )
        )


def test_the_old_allowed_gate_ids_field_is_gone():
    with pytest.raises(ValidationError):
        _registration(allowed_gate_ids=frozenset({"fake"}))


def test_a_registration_defaults_to_no_bypass_rules_and_every_recordable_action():
    reg = _registration()
    assert reg.bypass_rules == ()
    assert reg.recordable_actions == frozenset(DecisionAction)


def test_a_registration_looks_bypass_rules_up_by_id():
    low, high = _rule("low-score"), _rule("high-score", action=BLOCK)
    reg = _registration(bypass_rules=(low, high))
    assert reg.bypass_rule("high-score") == high
    assert reg.bypass_rule("missing") is None


def test_a_registration_rejects_duplicate_bypass_rule_ids():
    with pytest.raises(ValidationError, match="duplicate bypass rule_id"):
        _registration(bypass_rules=(_rule("r"), _rule("r", action=BLOCK)))


def test_every_bypass_rule_action_must_be_recordable():
    with pytest.raises(ValidationError, match="outside recordable_actions"):
        _registration(
            bypass_rules=(_rule("block-it", action=BLOCK),),
            recordable_actions=frozenset({ALLOW}),
        )


def test_a_registration_may_record_nothing_when_it_registers_no_rule():
    reg = _registration(recordable_actions=frozenset())
    assert reg.recordable_actions == frozenset()


def test_a_registration_carries_no_gate_specific_resource_sets():
    # DR-28: what a reasoner may put in a gate's configuration is the gate's
    # business; the registration has no template or jurisdiction allow-list.
    fields = set(ReasonerRegistration.model_fields)
    assert "allowed_prompt_template_ids" not in fields
    assert "allowed_jurisdictions" not in fields


def test_registration_enforcement_profile_defaults_to_disabled():
    reg = _registration()
    assert reg.novel_entity_feature is None
    assert reg.jurisdiction_conflict_groups == ()


def test_registration_accepts_an_enforcement_profile():
    reg = _registration(
        novel_entity_feature="thin_history",
        jurisdiction_conflict_groups=(frozenset({"A"}), frozenset({"B"})),
    )
    assert reg.novel_entity_feature == "thin_history"
    assert reg.jurisdiction_conflict_groups == (frozenset({"A"}), frozenset({"B"}))


def test_registration_rejects_non_frozenset_conflict_groups_in_strict_mode():
    with pytest.raises(ValidationError):
        _registration(jurisdiction_conflict_groups=({"A"},))


def test_a_registration_is_immutable():
    reg = _registration()
    with pytest.raises(ValidationError):
        reg.reasoner_id = "other"
    with pytest.raises(ValidationError):
        reg.gates = ()


def test_a_registration_round_trips_through_json():
    reg = _registration(
        gates=(GateBinding(gate_id="fake"), GateBinding(gate_id="lk", mandatory=True)),
        bypass_rules=(_rule("low-score"), _rule("high-score", action=BLOCK)),
        recordable_actions=frozenset({ALLOW, BLOCK}),
        novel_entity_feature="thin_history",
        jurisdiction_conflict_groups=(frozenset({"A", "B"}),),
    )
    assert ReasonerRegistration.model_validate_json(reg.model_dump_json()) == reg


# ---------------------------------------------------------------------------
# ReasonerRegistry protocol conformance
# ---------------------------------------------------------------------------


def test_registry_stub_satisfies_protocol():
    assert isinstance(_RegistryStub([_registration()]), ReasonerRegistry)


def test_registry_stub_is_registered_returns_true_for_known_id():
    assert _RegistryStub([_registration()]).is_registered("stub-reasoner")


def test_registry_stub_is_registered_returns_false_for_unknown_id():
    assert not _RegistryStub([_registration()]).is_registered("unknown-reasoner")


def test_registry_stub_get_raises_for_unknown_id():
    with pytest.raises(UnregisteredReasonerError) as exc_info:
        _RegistryStub([_registration()]).get("unknown-reasoner")
    assert exc_info.value.reasoner_id == "unknown-reasoner"


# ---------------------------------------------------------------------------
# registration_version — the content address every bundle records (DR-31)
# ---------------------------------------------------------------------------


def _profiled(**overrides: object) -> ReasonerRegistration:
    fields: dict[str, object] = {
        "bypass_rules": (_rule(), _rule("high-score", action=BLOCK)),
        "recordable_actions": frozenset({ALLOW, BLOCK}),
        "novel_entity_feature": "thin_history",
        "jurisdiction_conflict_groups": (
            frozenset({"REGION_A", "REGION_C"}),
            frozenset({"REGION_B"}),
        ),
    }
    fields.update(overrides)
    return _registration(**fields)


def test_a_registration_version_is_a_short_hex_digest():
    version = registration_version(_profiled())
    assert len(version) == 16
    assert int(version, 16) >= 0


def test_equal_registrations_have_equal_versions():
    reloaded = ReasonerRegistration.model_validate_json(_profiled().model_dump_json())
    assert registration_version(reloaded) == registration_version(_profiled())


def test_the_canonical_form_sorts_every_set():
    data = canonical_registration(_profiled())
    assert data["recordable_actions"] == ["ALLOW", "BLOCK"]
    assert data["jurisdiction_conflict_groups"] == [
        ["REGION_A", "REGION_C"],
        ["REGION_B"],
    ]
    # ...and reloads into an equal registration.
    assert ReasonerRegistration.model_validate_json(
        ReasonerRegistration.model_validate(data, strict=False).model_dump_json()
    ) == ReasonerRegistration.model_validate_json(_profiled().model_dump_json())


_VERSION_IN_A_SUBPROCESS = """
from core.actions import DecisionAction as A
from core.observation import GateBinding, ReasonerRegistration, registration_version
r = ReasonerRegistration(
    reasoner_id="stub-reasoner",
    reasoner_name="Test Reasoner",
    gates=(GateBinding(gate_id="fake"),),
    recordable_actions=frozenset({A.ALLOW, A.HOLD, A.BLOCK}),
    jurisdiction_conflict_groups=(frozenset({"X", "Y", "Z", "W"}),),
)
print(registration_version(r))
"""


def test_the_version_does_not_depend_on_the_process_hash_seed():
    versions = {
        subprocess.run(  # noqa: S603 (fixed interpreter and script)
            [sys.executable, "-c", _VERSION_IN_A_SUBPROCESS],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).stdout.strip()
        for seed in ("1", "2", "3", "4")
    }
    assert len(versions) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"reasoner_name": "Renamed"},
        {"gates": (GateBinding(gate_id="fake", mandatory=True),)},
        {"bypass_rules": (_rule(version="2"), _rule("high-score", action=BLOCK))},
        {"recordable_actions": frozenset({ALLOW, BLOCK, HOLD})},
        {"novel_entity_feature": None},
        {"jurisdiction_conflict_groups": (frozenset({"REGION_A"}),)},
    ],
    ids=["name", "gate-set", "rule-version", "recordable", "tier3", "tier5"],
)
def test_any_change_changes_the_version(change):
    assert registration_version(_profiled(**change)) != registration_version(
        _profiled()
    )
