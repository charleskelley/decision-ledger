"""Reasoner registration contracts — what the ledger knows about a reasoner.

A ``ReasonerRegistration`` is the framework's declaration of a known domain
reasoner:

- its identity (``reasoner_id``, ``reasoner_name``);
- its **gate set** (DR-29): one ``GateBinding`` per gate its decisions go
  through. A *mandatory* gate runs on every decision, recorded or gated; an
  *advisory* gate runs only when the reasoner asks the ledger to gate a
  decision. The deployment may add mandatory gates for every reasoner
  (``DECISIONLEDGER_MANDATORY_GATES``). A binding may carry the gate's
  configuration for this reasoner (``config``, DR-33), which the framework
  never interprets and the gate validates when it admits the binding;
- its **bypass rules** (DR-30): the registered, replayable conditions under
  which it may *record* a decision it made without asking the advisory
  gates. A recorded submission cites one by id; the ledger checks the
  citation, evaluates the rule's predicate against the submission's
  evidence, and replay evaluates it again;
- the actions it may record (``recordable_actions``);
- the native namespaces it names its subjects in (``native_namespaces``,
  DR-32): for each, the subject type it names and the UUID5 namespace the
  ledger derives those subjects' ids under when no identity resolver
  matches them;
- the enforcement profile the deterministic enforcement layer reads for the
  novel-entity tier (Tier 3) and the jurisdiction-conflict tier (Tier 5).

What a reasoner may put in a gate's configuration is that gate's business,
not the registration's (DR-28): a gate validates its own ``gate_config`` and
may authorize it per reasoner at intake, and it validates a binding's
``config`` when it admits the binding (DR-33).

A ``ReasonerRegistry`` is the protocol any registry implementation must
satisfy: the reads intake and replay use, and ``register`` for a
registration written at runtime (DR-33). The reference implementation
(``LiveReasonerRegistry`` in ``app/``) composes the deployment's declared
registrations (plugins and the registration file) with the ones written
over the API, which it keeps in Postgres; the framework is agnostic to the
backing store.

A registration is data (DR-31): ``registration_version`` content-addresses
it, every decision bundle records the version in effect when it was made,
and the ledger keeps a snapshot of each version, so replay enforces against
the profile the decision was made under rather than the one registered now.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from core.actions import DecisionAction
from core.gate import Predicate
from core.identity import NativeNamespace

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
        config: How the gate is configured for this reasoner (DR-33):
            JSON-shaped data the framework never interprets. The gate
            validates it (its plugin's ``binding_config_model`` and its
            ``validate_binding``) when the deployment starts and when the
            registration is written, and may refuse the binding. It is part
            of the registration, so it is versioned and snapshotted with it.
            Empty (the default) for a gate that takes no binding
            configuration. What a single decision asks of the gate stays in
            that submission's ``GateContext.gate_config``.
    """

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", allow_inf_nan=False
    )

    gate_id: str = Field(min_length=1)
    mandatory: bool = False
    config: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("config")
    @classmethod
    def _config_is_json(cls, config: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """JSON has no NaN or infinity, however the input spelled them."""
        if _holds_non_finite(config):
            msg = "config holds a non-finite number, which JSON cannot carry"
            raise ValueError(msg)
        return config

    def __hash__(self) -> int:
        """Hash by value, as equality compares: the config frozen recursively.

        A frozen model's generated hash fails on the ``config`` dict; this one
        keeps bindings (and registrations whose other fields hash) hashable,
        and agrees with ``==`` (key order is ignored, ``1`` equals ``1.0``).
        """
        return hash((self.gate_id, self.mandatory, _frozen(self.config)))


def _frozen(value: object) -> object:
    """A hashable form of a JSON value that is equal exactly when it is."""
    if isinstance(value, dict):
        return frozenset((key, _frozen(item)) for key, item in value.items())
    if isinstance(value, list):
        return tuple(_frozen(item) for item in value)
    return value


def _holds_non_finite(value: object) -> bool:
    """Whether a JSON-shaped value holds a NaN or an infinity, at any depth."""
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_holds_non_finite(item) for item in value.values())
    if isinstance(value, list):
        return any(_holds_non_finite(item) for item in value)
    return False


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
        native_namespaces: The native namespaces the reasoner names its
            subjects in (DR-32), each ``native_namespace`` once. A
            submission naming a subject in one of them must carry the
            ``entity_id`` and ``entity_type`` it declares, and the
            passthrough resolver records that subject when no resolver
            matches it. Empty (the default) declares none.
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
    native_namespaces: tuple[NativeNamespace, ...] = ()
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
        names = [namespace.native_namespace for namespace in self.native_namespaces]
        repeated_namespaces = sorted({n for n in names if names.count(n) > 1})
        if repeated_namespaces:
            msg = f"native namespace(s) declared more than once: {repeated_namespaces}"
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

    def native_namespace(self, name: str) -> NativeNamespace | None:
        """Return the declared native namespace ``name``, or ``None``."""
        return next(
            (n for n in self.native_namespaces if n.native_namespace == name), None
        )


def canonical_registration(registration: ReasonerRegistration) -> dict[str, Any]:
    """Return the registration as JSON-shaped data with every set sorted.

    ``model_dump(mode="json")`` writes a ``frozenset`` in iteration order,
    which for strings depends on the process's hash seed; the two set-valued
    fields (``recordable_actions`` and each jurisdiction conflict group) are
    sorted here so the same registration always has the same form. Tuples
    keep their order: gate bindings and bypass rules are ordered by meaning.
    Native namespace declarations have no order, so they are sorted by name;
    a registration that declares none has no ``native_namespaces`` key at
    all, so its form (and its version) is the one it had before
    registrations could declare them (DR-32). Likewise a gate binding with
    an empty ``config`` has no ``config`` key, so a registration whose
    bindings carry no configuration keeps the version it had before
    bindings could carry one (DR-33). A binding's configuration is data in
    its own right: lists inside it keep their order, and the version is
    computed with sorted keys, so the order of its keys never matters.

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
    if data["native_namespaces"]:
        data["native_namespaces"] = sorted(
            data["native_namespaces"], key=lambda n: n["native_namespace"]
        )
    else:
        del data["native_namespaces"]
    for binding in data["gates"]:
        if not binding["config"]:
            del binding["config"]
    return data


def registration_version(registration: ReasonerRegistration) -> str:
    """Return the content address of a registration (DR-31). Pure.

    The first ``REGISTRATION_VERSION_LENGTH`` hex digits of the SHA-256 of
    the canonical form (sorted keys, no whitespace). Equal registrations
    have equal versions in every process; any change to the gate set (a
    binding's configuration included), a bypass rule, the recordable
    actions, a native namespace declaration, or the enforcement profile
    changes it. Every decision bundle records the
    version its registration had, and replay reads the snapshot of that
    version.

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
    """Protocol for the reasoner registry the framework reads and writes.

    Any object that provides ``get``, ``is_registered``, and ``register``
    satisfies this protocol. Domain reasoners are not required to interact
    with the registry directly — it is a framework-side concern read at
    submission intake and by replay, and written by the registrations
    resource (``PUT /api/v1/reasoners/{reasoner_id}``, DR-33).

    The reference implementation (``LiveReasonerRegistry`` in ``app/``)
    composes the plugins' and the registration file's registrations at
    startup with the ones written over the API, which it keeps in Postgres.
    A production implementation could back it with another database or a
    remote config service.
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

    def register(
        self, registration: ReasonerRegistration, *, registered_by: str
    ) -> str:
        """Add a registration at runtime, or replace one added at runtime.

        The caller has admitted the registration first (the checks a
        deployment runs at startup). The registry keeps the version's
        snapshot before it serves the version, so every bundle's
        ``registration_version`` has one; an unchanged registration is left
        as it is.

        Args:
            registration: The registration to serve from now on.
            registered_by: Who wrote it (a principal label), recorded with it.

        Returns:
            Its ``registration_version``.

        Raises:
            RegistrationError: ``registration.reasoner_id`` is registered by
                the deployment's declared configuration (a plugin or the
                registration file), which cannot be replaced at runtime.
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
