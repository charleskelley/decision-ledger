"""Submission — the concrete wire contract a reasoner hands the ledger (DR-31).

A reasoner either asks the ledger to **gate** a decision it proposes, or to
**record** a decision it already made (DR-30). Both arrive as one strict,
frozen, JSON-serializable ``Submission``; there is no reasoner-specific
Python type at the boundary, so an in-process caller, an HTTP client, and an
agent calling a tool all hand over the same thing, and replay needs no
reasoner code (only the reasoner's registration, for its enforcement
profile).

A submission carries:

- identity: ``reasoner_id``, ``event_id``, ``timestamp``, the reasoner's
  own id for the subject (``entity_id``, ``entity_type``), and, optionally,
  the subject in the reasoner's own terms (``native_subject``, a
  ``core.identity.NativeSubject``), which the ledger resolves to the
  decision's subject through the deployment's identity resolver (DR-32);
- ``mode`` — ``record`` (the reasoner decided: ``proposed_action`` plus a
  ``bypass`` citation of one of its registered bypass rules) or ``gate``
  (the reasoner proposes, optionally, and the ledger runs its gates);
- ``reasoner_context`` — the reasoner's evidence (prediction and complete
  feature snapshot), which gates read and bypass rules are evaluated on;
- ``gate_contexts`` — one ``GateContext`` per bound gate, on every
  submission (recorded ones too, so any decision can later be evaluated by
  its advisory gates offline: shadow evaluation);
- ``raw_event`` — the reasoner's own event, opaque JSON stored verbatim.

Field-level rules are model validators; rules that need the reasoner's
registration live in ``validate_submission`` (a native subject in a namespace
the registration declares must match the reasoner's ``entity_id``, for one).
``matching_bypass_rule``, ``evaluate_bypass``, and
``evaluate_competing_rules`` are the pure helpers a reasoner and the ledger
share to pick and to check bypass rules.

Every value must survive the ledger's JSON round trip, because the bundle
stores the submission verbatim and replay reloads it: non-finite numbers, NUL
characters, and strings that are not valid Unicode are rejected anywhere in
it (``storage_problem``; the JSON values ``raw_event`` and ``gate_config``
included, which JSON input could otherwise fill with ``NaN``), unknown fields
are rejected at every level, and gate configuration is JSON. Parse wire input
with ``Submission.model_validate_json``: the models are strict, and strict
validation accepts JSON's representations (ISO timestamps, UUID strings, enum
values, arrays for tuples) only from JSON text.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)

from core.actions import DecisionAction
from core.exceptions import PredicateError, SubmissionContractError
from core.gate import evaluate_predicate, referenced_fields, severity
from core.gate.predicate import Scalar, resolve_field
from core.identity import NativeSubject
from core.observation import (
    BypassRule,
    GateBinding,
    GateContext,
    ReasonerContext,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from core.observation import ReasonerRegistration

    #: Builds the ``SubmissionContractError`` for one failed check.
    _FailFactory = Callable[[str, str], SubmissionContractError]

#: The wire schema version this module defines.
SUBMISSION_SCHEMA_VERSION = 2

#: ``record`` — the reasoner decided and cites a bypass rule; ``gate`` — the
#: reasoner asks the ledger to run its gates.
SubmissionMode = Literal["record", "gate"]


class BypassCitation(BaseModel):
    """The registered bypass rule a recorded decision cites.

    Attributes:
        rule_id: A ``BypassRule.rule_id`` from the reasoner's registration.
        rule_version: Optional pin. When set it must equal the registered
            rule's ``version``, so a reasoner built against an older rule
            cannot record under a newer one unnoticed.
    """

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", allow_inf_nan=False
    )

    rule_id: str = Field(min_length=1)
    rule_version: str | None = None


class Submission(BaseModel):
    """One decision a reasoner hands the ledger, to record or to gate.

    Attributes:
        schema_version: Wire schema version; always ``2``.
        reasoner_id: The submitting reasoner. Must equal
            ``reasoner_context.reasoner_id``.
        event_id: Reasoner-assigned unique id of the event the decision is
            about. The default idempotency key and the log correlation id.
        timestamp: When the event occurred (timezone-aware).
        entity_id: The reasoner's own UUID for the subject it scored
            (UUID5 of its native key under its namespace). The decision's
            subject is the identity the ledger resolves
            (``DecisionBundle.identity``, DR-32): the same id when no
            resolver matches the subject, an enterprise subject's when one
            does.
        entity_type: Classification of that subject (``"account"``,
            ``"customer"``, ...).
        native_subject: The subject in the reasoner's own terms (a native
            namespace and key), which the ledger resolves through the
            deployment's identity resolver; ``None`` when the reasoner names
            none (its ``entity_id`` is then recorded as the subject,
            unresolved). In a namespace the reasoner's registration
            declares, ``entity_id`` and ``entity_type`` must be the ones the
            declaration derives.
        mode: ``"record"`` or ``"gate"``.
        proposed_action: The reasoner's action. Required to record (it is
            the decision); optional to gate (a floor: the ledger's result is
            never less restrictive than a proposal).
        bypass: The bypass rule a record cites. Required exactly when
            ``mode == "record"``.
        reasoner_context: The reasoner's evidence: model identity, the
            prediction, and the complete feature snapshot it used.
        gate_contexts: One dispatch envelope per gate, each ``gate_id`` at
            most once.
        raw_event: The reasoner's event as JSON, stored verbatim and never
            interpreted by the ledger.
    """

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", allow_inf_nan=False
    )

    schema_version: Literal[2] = 2
    reasoner_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    timestamp: AwareDatetime
    entity_id: UUID
    entity_type: str = Field(min_length=1)
    native_subject: NativeSubject | None = None
    mode: SubmissionMode
    proposed_action: DecisionAction | None = None
    bypass: BypassCitation | None = None
    reasoner_context: ReasonerContext
    gate_contexts: tuple[GateContext, ...] = ()
    raw_event: dict[str, JsonValue]

    @model_validator(mode="after")
    def _mode_fields_are_consistent(self) -> Submission:
        """Records carry an action and a citation; gate requests no citation."""
        if self.mode == "record":
            if self.bypass is None:
                msg = "mode 'record' needs a bypass citation (the rule behind it)"
                raise ValueError(msg)
            if self.proposed_action is None:
                msg = "mode 'record' needs proposed_action (the recorded decision)"
                raise ValueError(msg)
        elif self.bypass is not None:
            msg = "mode 'gate' takes no bypass citation; the gates decide"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _one_reasoner_and_one_context_per_gate(self) -> Submission:
        """The evidence belongs to the submitter; each gate is addressed once."""
        if self.reasoner_context.reasoner_id != self.reasoner_id:
            msg = (
                f"reasoner_context.reasoner_id {self.reasoner_context.reasoner_id!r} "
                f"does not match reasoner_id {self.reasoner_id!r}"
            )
            raise ValueError(msg)
        gate_ids = [context.gate_id for context in self.gate_contexts]
        repeated = sorted({g for g in gate_ids if gate_ids.count(g) > 1})
        if repeated:
            msg = f"more than one gate context for gate(s) {repeated}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _storable(self) -> Submission:
        """Every value is one the ledger can store and reload as it is."""
        problem = storage_problem(self.model_dump(mode="json"))
        if problem is not None:
            msg = f"the submission holds {problem}, which the ledger cannot store"
            raise ValueError(msg)
        return self

    def gate_context(self, gate_id: str) -> GateContext | None:
        """Return the gate context addressed to ``gate_id``, if any."""
        return next((c for c in self.gate_contexts if c.gate_id == gate_id), None)


def storage_problem(value: object) -> str | None:
    """Describe the first value the ledger cannot store as it is, or ``None``.

    The ledger stores bundles as JSON in Postgres ``jsonb``: JSON has no
    non-finite number (a ``NaN`` reloads as ``null``), ``jsonb`` holds no NUL
    character, and a string that is not valid Unicode (a lone surrogate)
    cannot be encoded at all. Walks a JSON-shaped value such as
    ``model_dump(mode="json")`` — its dict keys and values, lists and
    tuples, strings, and floats.

    Args:
        value: The JSON-shaped value to check.

    Returns:
        What the first offending value is (``"a NUL character"``,
        ``"a non-finite number (nan)"``, ...), or ``None`` when every value
        can be stored.
    """
    if isinstance(value, str):
        if "\x00" in value:
            return "a NUL character"
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return "a string that is not valid Unicode (a lone surrogate)"
        return None
    if isinstance(value, float):
        return None if math.isfinite(value) else f"a non-finite number ({value!r})"
    if isinstance(value, dict):
        for key, item in value.items():
            problem = storage_problem(key) or storage_problem(item)
            if problem is not None:
                return problem
        return None
    if isinstance(value, list | tuple):
        for item in value:
            problem = storage_problem(item)
            if problem is not None:
                return problem
    return None


class BypassCheck(BaseModel):
    """The ledger's evaluation of one registered bypass rule for a record.

    Stored in the bundle with the rule itself — for the cited rule and for
    every other rule the reasoner registers — so replay re-evaluates the
    exact predicates that were checked, without the registration.

    Attributes:
        rule: The registered rule, as evaluated.
        observed: The submission's value for every field the predicate
            references (fields the evidence lacks are omitted).
        holds: Whether the predicate held against the evidence. ``False``
            when it could not be evaluated.
        error: Why the predicate could not be evaluated (a missing feature,
            a non-numeric operand), or ``None``.
    """

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", allow_inf_nan=False
    )

    rule: BypassRule
    observed: dict[str, Scalar] = Field(default_factory=dict)
    holds: bool
    error: str | None = None


def effective_gate_set(
    registration: ReasonerRegistration,
    deployment_mandatory: Iterable[str] = (),
) -> tuple[GateBinding, ...]:
    """Return the gates a decision for this reasoner goes through.

    The registration's bindings, in order, with any the deployment mandates
    upgraded to mandatory (keeping their ``config``); then every other
    deployment-mandatory gate, as a mandatory binding without
    configuration, sorted by id.

    Args:
        registration: The submitting reasoner's registration.
        deployment_mandatory: Gate ids the deployment runs on every decision
            (``DECISIONLEDGER_MANDATORY_GATES``).

    Returns:
        The gate set as bindings, each ``gate_id`` once.
    """
    mandated = frozenset(deployment_mandatory)
    bound = tuple(
        binding
        if binding.mandatory or binding.gate_id not in mandated
        else GateBinding(gate_id=binding.gate_id, mandatory=True, config=binding.config)
        for binding in registration.gates
    )
    extra = tuple(
        GateBinding(gate_id=gate_id, mandatory=True)
        for gate_id in sorted(mandated - registration.gate_ids)
    )
    return bound + extra


def validate_submission(
    submission: Submission,
    registration: ReasonerRegistration,
    *,
    deployment_mandatory: Iterable[str] = (),
) -> BypassRule | None:
    """Check a submission against its reasoner's registration.

    Shape rules are enforced by ``Submission`` itself; these need the
    registration. Gate configurations are checked separately, by the gates
    that own them.

    Args:
        submission: The validated submission.
        registration: The registration of ``submission.reasoner_id``.
        deployment_mandatory: Gate ids the deployment runs on every decision.

    A gate context may address only a gate the registration binds. A gate
    the deployment mandates without the registration binding it runs with
    the deployment's own (empty) configuration: the reasoner cannot steer a
    control it did not agree to.

    Returns:
        The registered bypass rule a record cites, or ``None`` in gate mode.

    Raises:
        SubmissionContractError: With ``code``
            ``BYPASS_RULE_UNREGISTERED``, ``ACTION_NOT_RECORDABLE``,
            ``BYPASS_ACTION_MISMATCH``, ``BYPASS_RULE_VERSION_MISMATCH``,
            ``NATIVE_SUBJECT_MISMATCH``, ``GATE_NOT_BOUND``,
            ``GATE_CONTEXT_MISSING``, or ``NO_GATES`` (see
            ``SubmissionContractError``), or ``SUBMISSION_INVALID`` when the
            registration is for another reasoner.
    """
    reasoner_id = submission.reasoner_id

    def _fail(code: str, violation: str) -> SubmissionContractError:
        return SubmissionContractError(reasoner_id, violation, code=code)

    if registration.reasoner_id != reasoner_id:
        raise _fail(
            "SUBMISSION_INVALID",
            f"registration is for {registration.reasoner_id!r}, not {reasoner_id!r}",
        )

    rule = _validate_citation(submission, registration, _fail)
    _validate_native_subject(submission, registration, _fail)

    gate_set = effective_gate_set(registration, deployment_mandatory)
    addressed = {context.gate_id for context in submission.gate_contexts}
    unbound = sorted(addressed - registration.gate_ids)
    if unbound:
        raise _fail(
            "GATE_NOT_BOUND",
            f"gate context(s) for {unbound} name no gate this reasoner binds "
            f"{sorted(registration.gate_ids)} (a gate the deployment mandates "
            "runs with the deployment's configuration)",
        )
    missing = sorted(registration.gate_ids - addressed)
    if missing:
        raise _fail(
            "GATE_CONTEXT_MISSING",
            f"no gate context for bound gate(s) {missing}; every submission "
            "addresses every gate its registration binds, recorded ones too",
        )
    if submission.mode == "gate" and not gate_set:
        raise _fail(
            "NO_GATES",
            "mode 'gate' needs a gate to evaluate it, but the reasoner binds none "
            "and the deployment mandates none",
        )
    return rule


def matching_bypass_rule(
    registration: ReasonerRegistration,
    reasoner_context: ReasonerContext,
) -> BypassRule | None:
    """Return the registered bypass rule a record should cite, if any holds.

    A reasoner uses this to decide whether to record (citing the rule, with
    its action) or to ask for a gate. Of the rules that hold, it returns the
    one with the most restrictive action (the first in registration order
    among equals) — the same rule the ledger would floor the decision at if
    a less restrictive one were cited (``evaluate_competing_rules``). A
    predicate that cannot be evaluated against the evidence does not hold.

    Args:
        registration: The reasoner's registration.
        reasoner_context: The evidence the submission will carry.

    Returns:
        The strictest holding rule, or ``None`` when none holds.
    """
    holding = [
        rule
        for rule in registration.bypass_rules
        if evaluate_bypass(rule, reasoner_context).holds
    ]
    if not holding:
        return None
    # max() keeps the first maximal element: registration order breaks ties.
    return max(holding, key=lambda rule: severity(rule.action))


def evaluate_bypass(rule: BypassRule, reasoner_context: ReasonerContext) -> BypassCheck:
    """Evaluate a bypass rule against a submission's evidence. Pure.

    Args:
        rule: The registered rule a record cites.
        reasoner_context: The submission's evidence.

    Returns:
        The rule, the observed values its predicate reads, and whether it
        held; a predicate that cannot be evaluated does not hold and records
        why.
    """
    observed: dict[str, Scalar] = {}
    for field in sorted(referenced_fields(rule.predicate)):
        try:
            observed[field] = resolve_field(field, reasoner_context)
        except PredicateError:
            continue
    try:
        holds = evaluate_predicate(rule.predicate, reasoner_context)
    except PredicateError as err:
        return BypassCheck(rule=rule, observed=observed, holds=False, error=err.reason)
    return BypassCheck(rule=rule, observed=observed, holds=holds)


def evaluate_competing_rules(
    registration: ReasonerRegistration,
    cited: BypassRule,
    reasoner_context: ReasonerContext,
) -> tuple[BypassCheck, ...]:
    """Evaluate every registered bypass rule other than the cited one. Pure.

    A record cites one rule, but the ledger checks them all: when another
    rule that also holds justifies a more restrictive action, the decision
    is floored at that action (the reasoner cannot pick the lenient one of
    two rules its own evidence satisfies).

    Args:
        registration: The reasoner's registration.
        cited: The rule the record cites.
        reasoner_context: The submission's evidence.

    Returns:
        One check per other registered rule, in registration order.
    """
    return tuple(
        evaluate_bypass(rule, reasoner_context)
        for rule in registration.bypass_rules
        if rule.rule_id != cited.rule_id
    )


def _validate_native_subject(
    submission: Submission,
    registration: ReasonerRegistration,
    fail: _FailFactory,
) -> None:
    """A native subject in a declared namespace matches the reasoner's own id.

    The registration's declaration of a namespace says which subject type it
    names and which UUID5 namespace its ids derive under (DR-32); a
    submission that names a subject there and a different ``entity_id`` or
    ``entity_type`` contradicts its own registration. A namespace the
    registration does not declare is not checked: the ledger records the
    reasoner's ``entity_id`` unless a resolver matches the subject.
    """
    subject = submission.native_subject
    if subject is None:
        return
    namespace = registration.native_namespace(subject.native_namespace)
    if namespace is None:
        return
    expected = namespace.entity_id_of(subject.native_subject_id)
    if (submission.entity_id, submission.entity_type) != (
        expected,
        namespace.entity_type,
    ):
        raise fail(
            "NATIVE_SUBJECT_MISMATCH",
            f"the native subject is in namespace {subject.native_namespace!r}, "
            f"which this reasoner's registration declares: its entity is "
            f"{expected} of type {namespace.entity_type!r}, but the submission "
            f"names {submission.entity_id} of type {submission.entity_type!r}",
        )


def _validate_citation(
    submission: Submission,
    registration: ReasonerRegistration,
    fail: _FailFactory,
) -> BypassRule | None:
    """Resolve and check a record's citation; ``None`` in gate mode."""
    citation = submission.bypass
    if submission.mode != "record" or citation is None:
        return None
    rule = registration.bypass_rule(citation.rule_id)
    if rule is None:
        registered = sorted(r.rule_id for r in registration.bypass_rules)
        raise fail(
            "BYPASS_RULE_UNREGISTERED",
            f"bypass rule {citation.rule_id!r} is not registered for this reasoner "
            f"(registered: {registered})",
        )
    proposed = submission.proposed_action
    if proposed not in registration.recordable_actions:
        raise fail(
            "ACTION_NOT_RECORDABLE",
            f"this reasoner may not record {proposed}; recordable: "
            f"{sorted(a.value for a in registration.recordable_actions)}",
        )
    if proposed != rule.action:
        raise fail(
            "BYPASS_ACTION_MISMATCH",
            f"bypass rule {rule.rule_id!r} justifies {rule.action}, not {proposed}",
        )
    if citation.rule_version is not None and citation.rule_version != rule.version:
        raise fail(
            "BYPASS_RULE_VERSION_MISMATCH",
            f"the record pins {rule.rule_id!r} version {citation.rule_version!r}; "
            f"the registered version is {rule.version!r}",
        )
    return rule


__all__ = [
    "SUBMISSION_SCHEMA_VERSION",
    "BypassCheck",
    "BypassCitation",
    "NativeSubject",
    "Submission",
    "SubmissionMode",
    "effective_gate_set",
    "evaluate_bypass",
    "evaluate_competing_rules",
    "matching_bypass_rule",
    "validate_submission",
]
