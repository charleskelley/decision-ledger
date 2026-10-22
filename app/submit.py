"""Submission intake — the framework half of every decision (DR-29 to DR-32).

``submit()`` takes a reasoner's ``Submission`` and runs everything the ledger
owns: intake validation, the decision's subject, the decision's gate set,
enforcement, the combinator, and the bundle write. It returns the persisted
``DecisionBundle``. The reasoner is responsible for everything before it:
its features, its scoring, and building the submission — including choosing
its mode: *record* a decision it made, citing a registered bypass rule, or
ask the ledger to *gate* one.

The steps, in order:

1. **Validate.** The reasoner must be registered; ``validate_submission``
   checks the record's citation (a registered rule, the rule's action, a
   recordable action, a matching version pin), a native subject in a
   namespace the registration declares against the submission's
   ``entity_id``, and the gate contexts (only gates the registration binds,
   one for every bound gate). Every gate in the set that is composed then
   checks its own context: its ``config_model`` and, when the built gate is
   a ``ConfigAuthorizer``, its per-reasoner authorization. Any failure
   raises ``SubmissionContractError`` before a gate runs; nothing is
   persisted.
2. **Identity** (DR-32). The deployment's identity step resolves the
   submission's native subject through the active resolver, under its time
   budget, and records the result on the bundle; the passthrough subject,
   unresolved and with the reason, when there is no resolver or it does not
   match, fails, or times out. It never refuses a decision. Every gate sees
   the resolved subject (``GateRequest.identity``).
3. **Gate set.** The registration's bindings plus the gates the deployment
   mandates (``ComposedGate.mandatory``); a mandated gate the reasoner does
   not bind runs with an empty configuration.
4. **Mandatory gates** run concurrently, on every decision.
5. **Advisory gates** run concurrently, only on a gated decision, and only
   if no mandatory gate already blocks it (BLOCK absorbs in the combinator,
   so skipping cannot change the result). The reasoner's own proposal never
   skips them: a proposed BLOCK on a gated decision still gets the gates'
   evidence, or a reasoner could block without gating and without a bypass
   rule. Each skipped gate is recorded with the reason.
6. **Fail closed.** A gate that raises, outlives its budget, returns an
   unusable output, or is not composed is recorded as ``failed`` and its
   enforcement holds the decision (``app.gates.invoke_gate``).
7. **Enforce and combine.** Gate-level tiers per gate as it returns, then
   the decision-level rules and the combinator
   (``app.enforcement.resolver.resolve_decision``); a record's cited rule,
   and every other rule its reasoner registers, are evaluated against its
   evidence and snapshotted into the bundle.
8. **Write** the version-2 bundle, stamped with the
   ``registration_version`` of the registration intake used (DR-31), so
   replay enforces against that version's snapshot.

``decide()`` runs steps 1-7 and returns the bundle without writing it (the
batch route writes many in one round trip); ``submit()`` is ``decide()``
plus the write. Both are what the ledger's API operations call, so an HTTP
request, a ``LocalClient`` call, and an eval run decide identically.

Async (DR-23): the identity step and the gates are the only awaits (a
resolver, synchronous by contract, runs in a worker thread); enforcement and
the store are synchronous and run inline. The function logs no per-step
events of its own beyond what the identity step, the gates, and enforcement
log — callers log what makes sense for their context (HTTP request, eval
run, etc.).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING

from pydantic import ValidationError

from app.audit import build_bundle
from app.enforcement.resolver import resolve_decision, resolve_gate_output
from app.gates import ConfigAuthorizer, invoke_gate
from app.identity import IdentityService
from app.monitoring import duration_ms
from core.actions import DecisionAction
from core.bundle import GateRecord
from core.exceptions import SubmissionContractError
from core.gate import GateInput
from core.gate_request import GateRequest
from core.observation import GateContext, registration_version
from core.submission import (
    effective_gate_set,
    evaluate_bypass,
    evaluate_competing_rules,
    validate_submission,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime
    from typing import Any

    from pydantic import BaseModel

    from app.audit import BundleStore
    from app.gates import ComposedGate
    from core.bundle import DecisionBundle
    from core.observation import GateBinding, ReasonerRegistration, ReasonerRegistry
    from core.submission import Submission

    #: Builds the ``GateRequest`` for one gate id.
    _RequestFactory = Callable[[str], GateRequest]

#: Wall-clock budget granted to each gate invocation when the caller passes
#: none, in milliseconds. Enforced: a gate still running at the deadline is
#: cancelled and fails closed (DR-29). It bounds a hung gate and is longer
#: than one 30 s attempt of the reference RAG gate's model call, so one slow
#: call completes and the gate records its own outcome. A call its provider
#: SDK retries can outlast the budget; the framework then fails the gate
#: closed (HOLD) and the bundle has no gate forensics for it.
DEFAULT_GATE_BUDGET_MS = 45_000

#: The identity step of a caller that passes none: the passthrough alone.
_PASSTHROUGH_ONLY = IdentityService()

#: Why an advisory gate did not run on a recorded decision.
SKIPPED_FOR_RECORD = "mode=record: a recorded decision runs its mandatory gates only"
#: Why an advisory gate did not run once a mandatory gate blocked the decision.
SKIPPED_FOR_BLOCK = (
    "short-circuit: a mandatory gate already blocks the decision, so no "
    "advisory gate can change it"
)


async def submit(
    *,
    submission: Submission,
    gates: Mapping[str, ComposedGate],
    store: BundleStore,
    registry: ReasonerRegistry,
    ingestion_timestamp: datetime,
    upstream_latency: dict[str, float] | None = None,
    decision_id: str | None = None,
    idempotency_key: str | None = None,
    gate_budget_ms: int = DEFAULT_GATE_BUDGET_MS,
    identity: IdentityService | None = None,
) -> DecisionBundle:
    """Run the framework half of a decision on a submission and write it.

    ``decide()`` followed by ``store.write``; see ``decide`` for the
    arguments and the intake errors.

    Args:
        submission: The reasoner's submission.
        gates: The deployment's composed gates.
        store: Bundle persistence store (Postgres-backed).
        registry: Registry of known reasoners.
        ingestion_timestamp: When the ledger (or, in process, the
            reasoner's pipeline) received the event.
        upstream_latency: Optional latency breakdown of the caller's steps.
        decision_id: Optional explicit UUID; generated when omitted.
        idempotency_key: Optional explicit key; defaults to
            ``submission.event_id``.
        gate_budget_ms: Wall-clock budget for each gate invocation.
        identity: The deployment's identity step
            (``FrameworkServices.identity``); the passthrough alone when
            omitted.

    Returns:
        The version-2 ``DecisionBundle`` after persistence.

    Raises:
        UnregisteredReasonerError: ``submission.reasoner_id`` is not in
            ``registry``.
        SubmissionContractError: The submission violates the intake
            contract.

    Side effects:
        - Calls the identity resolver and the gates of the decision's gate
          set.
        - Writes the bundle to Postgres.
    """
    bundle = await decide(
        submission=submission,
        gates=gates,
        registry=registry,
        ingestion_timestamp=ingestion_timestamp,
        upstream_latency=upstream_latency,
        decision_id=decision_id,
        idempotency_key=idempotency_key,
        gate_budget_ms=gate_budget_ms,
        identity=identity,
    )
    store.write(bundle)
    return bundle


async def decide(
    *,
    submission: Submission,
    gates: Mapping[str, ComposedGate],
    registry: ReasonerRegistry,
    ingestion_timestamp: datetime,
    upstream_latency: dict[str, float] | None = None,
    decision_id: str | None = None,
    idempotency_key: str | None = None,
    gate_budget_ms: int = DEFAULT_GATE_BUDGET_MS,
    identity: IdentityService | None = None,
) -> DecisionBundle:
    """Decide a submission — intake, identity, the gate set, enforcement — unwritten.

    Args:
        submission: The reasoner's submission (already a validated
            ``Submission``: its shape rules hold).
        gates: The deployment's composed gates, keyed by ``gate_id``
            (``FrameworkServices.gates``). Each is flagged when the
            deployment mandates it for every decision.
        registry: Registry of known reasoners. The submitting reasoner must
            be registered; its registration supplies the gate set, the
            bypass rules, and the enforcement profile, and its
            ``registration_version`` is stamped on the bundle.
        ingestion_timestamp: When the ledger received the submission (an
            in-process caller may pass when the event entered its own
            pipeline). Recorded in the bundle.
        upstream_latency: Optional latency breakdown for the steps the
            reasoner ran before this call (e.g., ``{"features_ms": 1.2,
            "scorer_ms": 3.1}``). Merged into the bundle's
            ``latency_breakdown``.
        decision_id: Optional explicit UUID; generated when omitted.
        idempotency_key: Optional explicit key; defaults to
            ``submission.event_id``.
        gate_budget_ms: Wall-clock budget for each gate invocation.
        identity: The deployment's identity step
            (``FrameworkServices.identity``): the active resolver, its
            budget, and the passthrough fallback. The passthrough alone when
            omitted. Its result is the bundle's ``identity`` and every
            gate's ``GateRequest.identity``.

    Returns:
        The version-2 ``DecisionBundle``, not yet persisted.

    Raises:
        UnregisteredReasonerError: ``submission.reasoner_id`` is not in
            ``registry``.
        SubmissionContractError: The submission violates the intake
            contract (see ``SubmissionContractError`` for the codes).

    Side effects:
        - Calls the identity resolver (in a worker thread, bounded) and the
          gates of the decision's gate set (each may reach its own
          infrastructure, such as an index or an LLM API).
    """
    registration = registry.get(submission.reasoner_id)
    deployment_mandatory = frozenset(g for g, c in gates.items() if c.mandatory)
    rule = validate_submission(
        submission, registration, deployment_mandatory=deployment_mandatory
    )
    gate_set = effective_gate_set(registration, deployment_mandatory)
    contexts = {
        binding.gate_id: submission.gate_context(binding.gate_id)
        or GateContext(gate_id=binding.gate_id)
        for binding in gate_set
    }
    for binding in gate_set:
        composed = gates.get(binding.gate_id)
        if composed is not None:
            _validate_gate_config(
                composed,
                contexts[binding.gate_id],
                reasoner_id=submission.reasoner_id,
                bound=binding.gate_id in registration.gate_ids,
            )

    decision_id = decision_id or str(uuid.uuid4())
    idempotency_key = idempotency_key or submission.event_id
    latency: dict[str, float] = dict(upstream_latency or {})

    t0 = time.perf_counter()
    subject = await (identity or _PASSTHROUGH_ONLY).identify(
        submission, registration, decision_id=decision_id
    )
    latency["identity_ms"] = duration_ms(t0)

    bypass_check = (
        evaluate_bypass(rule, submission.reasoner_context) if rule is not None else None
    )
    competing_checks = (
        evaluate_competing_rules(registration, rule, submission.reasoner_context)
        if rule is not None
        else ()
    )

    binding_configs = {binding.gate_id: binding.config for binding in gate_set}

    def _request(gate_id: str) -> GateRequest:
        return GateRequest(
            decision_id=decision_id,
            submission=submission,
            gate_context=contexts[gate_id],
            budget_ms=gate_budget_ms,
            identity=subject,
            binding_config=dict(binding_configs.get(gate_id, {})),
        )

    mandatory = [b for b in gate_set if b.mandatory]
    advisory = [b for b in gate_set if not b.mandatory]

    t0 = time.perf_counter()
    records = await run_gate_set(
        mandatory,
        gates=gates,
        request=_request,
        registration=registration,
        decision_id=decision_id,
    )
    if submission.mode == "record":
        records += [_skipped(b, SKIPPED_FOR_RECORD) for b in advisory]
    elif _blocked_by_mandatory_gate(records):
        records += [_skipped(b, SKIPPED_FOR_BLOCK) for b in advisory]
    else:
        records += await run_gate_set(
            advisory,
            gates=gates,
            request=_request,
            registration=registration,
            decision_id=decision_id,
        )
    latency["gate_ms"] = duration_ms(t0)

    t0 = time.perf_counter()
    decision = resolve_decision(
        submission=submission,
        records=records,
        registration=registration,
        bypass_check=bypass_check,
        competing_checks=competing_checks,
        decision_id=decision_id,
    )
    latency["enforcement_ms"] = duration_ms(t0)

    # Bundle assembly + write latency is intentionally not tracked — the
    # bundle is frozen at build_bundle() time, so any post-construction
    # measurement cannot land in the persisted record. Keeping the response
    # total and the persisted total identical matters more than capturing
    # the bundle write time.
    return build_bundle(
        decision_id=decision_id,
        submission=submission,
        identity=subject,
        idempotency_key=idempotency_key,
        ingestion_timestamp=ingestion_timestamp,
        gates=records,
        decision=decision,
        bypass_check=bypass_check,
        other_bypass_checks=competing_checks,
        latency_breakdown=latency,
        registration_version=registration_version(registration),
    )


async def run_gate_set(
    bindings: Sequence[GateBinding],
    *,
    gates: Mapping[str, ComposedGate],
    request: _RequestFactory,
    registration: ReasonerRegistration,
    decision_id: str,
) -> list[GateRecord]:
    """Invoke every bound gate concurrently and enforce each output.

    Gates are independent (DR-29): each gets the same submission and its
    own context, none sees another's output, and the records come back in
    ``bindings`` order whatever order the gates finish in.

    Args:
        bindings: The gates to run.
        gates: The deployment's composed gates.
        request: Builds the ``GateRequest`` for a gate id.
        registration: The submitting reasoner's registration (gate-level
            Tier 5 reads it).
        decision_id: The decision, for log correlation.

    Returns:
        One ``ran`` or ``failed`` record per binding, gate-level
        enforcement included.
    """

    async def _one(binding: GateBinding) -> GateRecord:
        invocation = await invoke_gate(
            gates.get(binding.gate_id), request(binding.gate_id)
        )
        enforcement = resolve_gate_output(
            invocation.output,
            registration=registration,
            decision_id=decision_id,
            failure=invocation.reason,
        )
        return GateRecord(
            gate_id=binding.gate_id,
            mandatory=binding.mandatory,
            status="ran" if invocation.ran else "failed",
            reason=invocation.reason,
            input=GateInput(gate_id=binding.gate_id) if invocation.invoked else None,
            output=invocation.output,
            enforcement=enforcement,
            duration_ms=invocation.duration_ms,
        )

    return list(await asyncio.gather(*(_one(b) for b in bindings)))


def _skipped(binding: GateBinding, reason: str) -> GateRecord:
    return GateRecord(
        gate_id=binding.gate_id,
        mandatory=binding.mandatory,
        status="skipped",
        reason=reason,
    )


def _blocked_by_mandatory_gate(records: Sequence[GateRecord]) -> bool:
    """Whether a mandatory gate's enforced action is already BLOCK.

    BLOCK absorbs in the combinator, so no advisory outcome can change the
    decision then. The proposal is deliberately not consulted (see step 4).
    """
    return any(r.enforced_action == DecisionAction.BLOCK for r in records)


def _validate_gate_config(
    composed: ComposedGate,
    gate_context: GateContext,
    *,
    reasoner_id: str,
    bound: bool,
) -> None:
    """Validate a gate context for the gate it addresses, for this reasoner.

    Two checks, both owned by the gate: its declared ``config_model`` (the
    shape), then — when the built gate is a ``ConfigAuthorizer`` — whether
    this reasoner may send that configuration. Runs in both modes, so a
    recorded decision always carries contexts its gates could evaluate for
    shadow evaluation.

    Args:
        composed: The composed gate.
        gate_context: The context addressed to it (an empty one for a gate
            the deployment mandates that the reasoner does not bind).
        reasoner_id: The submitting reasoner.
        bound: Whether the reasoner's registration binds the gate.

    Raises:
        SubmissionContractError: ``GATE_CONFIG_INVALID`` when the context
            does not satisfy the gate's ``config_model``;
            ``GATE_CONFIG_UNAUTHORIZED`` when the gate refuses it for this
            reasoner.
    """
    raw = gate_context.gate_config or {}
    config: BaseModel | Mapping[str, Any] = raw
    config_model = composed.plugin.config_model
    if config_model is not None:
        try:
            config = config_model.model_validate(raw)
        except ValidationError as err:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in e['loc']) or '<root>'}: {e['msg']}"
                for e in err.errors()
            )
            hint = (
                ""
                if bound
                else " (the deployment mandates this gate and the reasoner sent "
                "no context for it; bind it in the registration to configure it)"
            )
            raise SubmissionContractError(
                reasoner_id,
                (
                    f"gate_config for gate {composed.gate_id!r} does not satisfy "
                    f"{config_model.__name__}: {problems}{hint}"
                ),
                code="GATE_CONFIG_INVALID",
            ) from err

    gate = composed.gate
    if not isinstance(gate, ConfigAuthorizer):
        return
    try:
        gate.authorize_config(config, reasoner_id=reasoner_id)
    except ValueError as err:
        raise SubmissionContractError(
            reasoner_id,
            (
                f"gate_config for gate {composed.gate_id!r} is not authorized "
                f"for this reasoner: {err}"
            ),
            code="GATE_CONFIG_UNAUTHORIZED",
        ) from err
