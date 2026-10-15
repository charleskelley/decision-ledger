"""Reasoner registration contracts — what the ledger knows about a reasoner.

A ``ReasonerRegistration`` is the framework's declaration of a known domain
reasoner:

- its identity (``reasoner_id``, ``reasoner_name``);
- its **gate set** (DR-29): one ``GateBinding`` per gate its decisions go
  through. A *mandatory* gate runs on every decision, recorded or gated; an
  *advisory* gate runs only when the reasoner asks the ledger to gate a
  decision. The deployment may add mandatory gates for every reasoner
  (``DECISIONLEDGER_MANDATORY_GATES``);
- its **bypass rules** (DR-30): the registered, replayable conditions under
  which it may *record* a decision it made without asking the advisory
  gates. A recorded submission cites one by id; the ledger checks the
  citation, evaluates the rule's predicate against the submission's
  evidence, and replay evaluates it again;
- the actions it may record (``recordable_actions``);
- the enforcement profile the deterministic enforcement layer reads for the
  novel-entity tier (Tier 3) and the jurisdiction-conflict tier (Tier 5).

What a reasoner may put in a gate's configuration is that gate's business,
not the registration's (DR-28): a gate validates its own ``gate_config`` and
may authorize it per reasoner at intake.

A ``ReasonerRegistry`` is the protocol any registry implementation must
satisfy. The reference implementation (``StaticReasonerRegistry`` in
``app/``) loads from a list at startup; a production implementation could
back it with a database — the framework is agnostic to the backing store.

A registration is data (DR-31): ``registration_version`` content-addresses
it, every decision bundle records the version in effect when it was made,
and the ledger keeps a snapshot of each version, so replay enforces against
the profile the decision was made under rather than the one registered now.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.actions import DecisionAction
from core.gate import Predicate

#: Hex digits of the SHA-256 digest a registration version keeps (64 bits).
REGISTRATION_VERSION_LENGTH = 16


class GateBinding(BaseModel):
    """One gate in a reasoner's gate set.

    Attributes:
        gate_id: The gate (a composed ``GatePlugin``'s ``gate_id``). Every
            submission from this reasoner must carry a ``GateContext`` for
            it — on recorded decisions too, so any decision can later be
            evaluated by its advisory gates offline (shadow evaluation).
        mandatory: ``True`` — the gate runs on every decision, including
            recorded ones. ``False`` (advisory) — the gate runs only when
            the reasoner submits in ``gate`` mode.
    """

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", allow_inf_nan=False
    )

    gate_id: str = Field(min_length=1)
    mandatory: bool = False


class BypassRule(BaseModel):
    """A registered condition under which a reasoner may record a decision.

    A recorded submission cites a rule by ``rule_id``. The ledger accepts
    the citation only when the rule is registered and the submission's
    ``proposed_action`` is the rule's ``action``; it then evaluates
    ``predicate`` against the submission's ``reasoner_context`` (the
    ``label_value`` and ``feature_set`` the reasoner reported), records
    whether it holds, and snapshots the rule into the bundle so replay can
    evaluate it again from the bundle alone. The predicate language is the
    one the rules gate uses (``core.gate.predicate``): pure, closed, and
    JSON-serializable.

    Attributes:
        rule_id: Identifier a recorded submission cites; unique within the
            registration.
        version: Version label recorded with every decision that cites the
            rule. Change it whenever the predicate or action changes.
        description: Human-readable intent of the rule.
        predicate: When the rule applies, over ``label_value`` and
            ``feature_set.<name>``.
        action: The one action the rule justifies recording. A reasoner
            that may record different actions under different conditions
            registers one rule per action.
    """

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", allow_inf_nan=False
    )

    rule_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    description: str
    predicate: Predicate
    action: DecisionAction


class ReasonerRegistration(BaseModel):
    """Framework declaration of a known domain reasoner.

    Attributes:
        reasoner_id: Stable machine identifier. Must match
            ``Submission.reasoner_id`` (and its ``reasoner_context``) on
            every submission this reasoner makes. Used as the registry key
            and as the bundle store's tenant column.
        reasoner_name: Human-readable display name for dashboards and audit
            reports (e.g., ``"Claims Reasoner"``).
        gates: The reasoner's gate set, in evaluation order: one
            ``GateBinding`` per gate, each ``gate_id`` at most once. A
            submission's gate contexts must name exactly the bound gates. A
            gate the deployment mandates without a binding here still runs,
            with an empty configuration, but the reasoner cannot address it
            (``GATE_NOT_BOUND``).
        bypass_rules: Rules a recorded submission may cite; ``rule_id``
            values are unique. Empty means the reasoner can only ask for a
            gate (its records would have nothing to cite).
        recordable_actions: Actions the reasoner may record. Every bypass
            rule's ``action`` must be one of them. Defaults to every action.
        novel_entity_feature: Enforcement profile for Tier 3 (novel
            entity). Names the ``ReasonerContext.feature_set`` key whose
            truthy value signals that the decision subject has too little
            history for a confident decision; on a gated decision,
            enforcement holds the advisory gates' verdicts for review.
            ``None`` (the default) means the reasoner exposes no such
            signal and Tier 3 never fires.
        jurisdiction_conflict_groups: Enforcement profile for Tier 5
            (jurisdiction conflict). Each member is a group of jurisdiction
            tags whose policies are mutually consistent; enforcement treats
            a gate's output as conflicted when its evidence spans at least
            two distinct groups and routes it to ``HOLD``. An empty tuple
            (the default) disables the structural check — Tier 5 then fires
            only when the gate itself signals a conflict.
    """

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", allow_inf_nan=False
    )

    reasoner_id: str
    reasoner_name: str
    gates: tuple[GateBinding, ...]
    bypass_rules: tuple[BypassRule, ...] = ()
    recordable_actions: frozenset[DecisionAction] = frozenset(DecisionAction)
    novel_entity_feature: str | None = None
    jurisdiction_conflict_groups: tuple[frozenset[str], ...] = ()

    @model_validator(mode="after")
    def _gate_set_and_rules_are_consistent(self) -> ReasonerRegistration:
        """Each gate is bound once, each rule id is unique, and rules are recordable."""
        gate_ids = [binding.gate_id for binding in self.gates]
        repeated_gates = sorted({g for g in gate_ids if gate_ids.count(g) > 1})
        if repeated_gates:
            msg = f"gate(s) bound more than once: {repeated_gates}"
            raise ValueError(msg)
        rule_ids = [rule.rule_id for rule in self.bypass_rules]
        repeated_rules = sorted({r for r in rule_ids if rule_ids.count(r) > 1})
        if repeated_rules:
            msg = f"duplicate bypass rule_id(s): {repeated_rules}"
            raise ValueError(msg)
        unrecordable = sorted(
            rule.rule_id
            for rule in self.bypass_rules
            if rule.action not in self.recordable_actions
        )
        if unrecordable:
            msg = (
                f"bypass rule(s) {unrecordable} justify an action outside "
                f"recordable_actions {sorted(a.value for a in self.recordable_actions)}"
            )
            raise ValueError(msg)
        return self

    @property
    def gate_ids(self) -> frozenset[str]:
        """Every gate the registration binds."""
        return frozenset(binding.gate_id for binding in self.gates)

    def binding(self, gate_id: str) -> GateBinding | None:
        """Return the binding for ``gate_id``, or ``None`` when it is not bound."""
        return next((b for b in self.gates if b.gate_id == gate_id), None)

    def bypass_rule(self, rule_id: str) -> BypassRule | None:
        """Return the registered bypass rule ``rule_id``, or ``None``."""
        return next((r for r in self.bypass_rules if r.rule_id == rule_id), None)


def canonical_registration(registration: ReasonerRegistration) -> dict[str, Any]:
    """Return the registration as JSON-shaped data with every set sorted.

    ``model_dump(mode="json")`` writes a ``frozenset`` in iteration order,
    which for strings depends on the process's hash seed; the two set-valued
    fields (``recordable_actions`` and each jurisdiction conflict group) are
    sorted here so the same registration always has the same form. Tuples
    keep their order: gate bindings and bypass rules are ordered by meaning.

    Args:
        registration: The registration.

    Returns:
        A JSON-serializable dict that reloads into an equal registration.
    """
    data = registration.model_dump(mode="json")
    data["recordable_actions"] = sorted(data["recordable_actions"])
    data["jurisdiction_conflict_groups"] = [
        sorted(group) for group in data["jurisdiction_conflict_groups"]
    ]
    return data


def registration_version(registration: ReasonerRegistration) -> str:
    """Return the content address of a registration (DR-31). Pure.

    The first ``REGISTRATION_VERSION_LENGTH`` hex digits of the SHA-256 of
    the canonical form (sorted keys, no whitespace). Equal registrations
    have equal versions in every process; any change to the gate set, a
    bypass rule, the recordable actions, or the enforcement profile changes
    it. Every decision bundle records the version its registration had, and
    replay reads the snapshot of that version.

    Args:
        registration: The registration.

    Returns:
        A 16-character lowercase hex string.
    """
    text = json.dumps(
        canonical_registration(registration),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return digest[:REGISTRATION_VERSION_LENGTH]


@runtime_checkable
class ReasonerRegistry(Protocol):
    """Protocol for reasoner registries the framework depends on at intake.

    Any object that provides ``get`` and ``is_registered`` satisfies this
    protocol. Domain reasoners are not required to interact with the
    registry directly — it is a framework-side concern used at submission
    intake and by replay.

    The reference implementation (``StaticReasonerRegistry`` in ``app/``)
    loads from a list at startup. A production implementation could back
    this with a database or remote config service.
    """

    def get(self, reasoner_id: str) -> ReasonerRegistration:
        """Return the registration for the given reasoner_id.

        Args:
            reasoner_id: The stable reasoner identifier to look up.

        Returns:
            The ``ReasonerRegistration`` for this reasoner.

        Raises:
            UnregisteredReasonerError: If ``reasoner_id`` is not registered.
        """
        ...

    def is_registered(self, reasoner_id: str) -> bool:
        """Return True if reasoner_id has a registration entry.

        Args:
            reasoner_id: The stable reasoner identifier to check.

        Returns:
            ``True`` if registered, ``False`` otherwise.
        """
        ...


__all__ = [
    "REGISTRATION_VERSION_LENGTH",
    "BypassRule",
    "GateBinding",
    "ReasonerRegistration",
    "ReasonerRegistry",
    "canonical_registration",
    "registration_version",
]
