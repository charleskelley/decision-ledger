"""Framework decision orchestrator — reasoner-agnostic half of the pipeline.

``execute_decision()`` runs the framework's portion of the pipeline against a
fully-assembled ``Observation``: intake validation against the reasoner
registry, the named gate's declared configuration model, and — when the
built gate is a ``ConfigAuthorizer`` — that gate's per-reasoner
authorization; dispatch to the gate the observation's ``GateContext`` names
(when the reasoner routed ``ROUTE_TO_GATE``), deterministic enforcement,
bundle assembly, and persistence. Returns the persisted ``DecisionBundle``.

The reasoner pipeline is responsible for everything *before* this function:
feature computation, scoring, and assembly — including every input its gate
needs in ``GateContext.gate_config``. Once it has those, it calls
``execute_decision()`` with the assembled observation and the deployment's
composed gates. This is the single handoff point between any reasoner and the
framework.

Gates are plugins (DR-28). The framework knows a gate only through
``ComposedGate`` (its plugin metadata and runtime ``Gate``), hands it a
``GateRequest``, and stores the universal ``GateOutput`` it returns. A gate
that returns something else, misstates its ``gate_id``, or misstates its
``deterministic`` flag is rejected with ``GatePluginError``.

Async (DR-23). The single ``await`` point is the gate call; enforcement and
the store are sync and run inline. The function itself does not log
structured per-step events — callers log what makes sense for their context
(HTTP request, eval run, etc.).
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING

from pydantic import ValidationError

from app.audit import build_bundle
from app.enforcement.resolver import resolve
from app.gates import ConfigAuthorizer
from app.monitoring import duration_ms
from core.exceptions import GatePluginError, ObservationContractError
from core.gate import EvidenceRef, GateInput, GateOutput, GateVerdict
from core.gate_request import GateRequest
from core.observation import validate_observation
from core.routes import GateRoute

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from typing import Any

    from pydantic import BaseModel

    from app.audit import BundleStore
    from app.gates import ComposedGate
    from core.bundle import DecisionBundle
    from core.observation import GateContext, Observation, ReasonerRegistry

#: Wall-clock budget granted to a gate invocation when the caller passes none.
#: Recorded on every ``GateRequest``; not yet enforced (DR-29 adds fail-closed
#: timeout handling with the gate set and combinator).
DEFAULT_GATE_BUDGET_MS = 30_000


async def execute_decision(
    *,
    observation: Observation,
    gates: Mapping[str, ComposedGate],
    store: BundleStore,
    registry: ReasonerRegistry,
    ingestion_timestamp: datetime,
    upstream_latency: dict[str, float] | None = None,
    decision_id: str | None = None,
    idempotency_key: str | None = None,
    gate_budget_ms: int = DEFAULT_GATE_BUDGET_MS,
) -> DecisionBundle:
    """Run the framework half of the decision pipeline against an Observation.

    The reasoner is expected to have already run feature computation,
    scoring, and ``build_observation`` (or its equivalent) before calling
    this function. The framework first validates the observation against
    the intake contract, the reasoner registry, and — when the named gate is
    composed — the gate's ``config_model`` (if it declares one) and the
    gate's per-reasoner authorization (if it is a ``ConfigAuthorizer``). On
    the gate route it dispatches to that gate; then it enforces, assembles
    the bundle, and persists it.

    Args:
        observation: Fully-assembled Observation from the reasoner.
            Must satisfy the ``core.observation.Observation`` protocol —
            ``reasoner_context`` and ``gate_context`` populated.
        gates: The deployment's composed gates, keyed by ``gate_id``
            (``FrameworkServices.gates``). The gate the observation names
            must be present when the observation routes to a gate.
        store: Bundle persistence store (Postgres-backed).
        registry: Registry of known reasoners. The observation's
            ``reasoner_id`` must be registered; its registration supplies
            the authorization checks at intake and the enforcement profile
            (Tiers 3 and 5) at resolution time.
        ingestion_timestamp: Wall-clock time at which the upstream raw event
            entered the pipeline. Recorded in the bundle.
        upstream_latency: Optional latency breakdown for steps the reasoner
            ran before this call (e.g., ``{"features_ms": 1.2,
            "scorer_ms": 3.1}``). Merged into the bundle's
            ``latency_breakdown``.
        decision_id: Optional explicit UUID; auto-generated when omitted.
        idempotency_key: Optional explicit key; defaults to
            ``observation.event_id``.
        gate_budget_ms: Wall-clock budget recorded on the ``GateRequest``.

    Returns:
        The fully-assembled ``DecisionBundle`` after persistence.

    Raises:
        ObservationContractError: The observation violates an intake
            contract (shape, gate authorization, the named gate's
            ``config_model``, or that gate's per-reasoner authorization).
        UnregisteredReasonerError: The observation's ``reasoner_id`` is not
            in ``registry``.
        GatePluginError: The observation routes to a gate this deployment
            does not compose, or the gate returned an output that is not a
            plain ``GateOutput`` for itself with its declared
            ``deterministic`` flag.

    Side effects:
        - Calls the gate (which may reach its own infrastructure, such as an
          index or an LLM API) when routed to the gate.
        - Writes the bundle to Postgres.
    """
    validate_observation(observation, registry=registry)
    # validate_observation guarantees reasoner_context is populated.
    assert observation.reasoner_context is not None
    reasoner_id = observation.reasoner_context.reasoner_id
    registration = registry.get(reasoner_id)

    gate_context = observation.gate_context
    composed = gates.get(gate_context.gate_id)
    routes_to_gate = observation.route == GateRoute.ROUTE_TO_GATE
    if composed is None and routes_to_gate:
        raise GatePluginError(
            gate_context.gate_id,
            f"observation routes to gate {gate_context.gate_id!r}, which this "
            f"deployment does not compose (composed gates: {sorted(gates)})",
        )
    if composed is not None:
        _validate_gate_config(composed, gate_context, reasoner_id=reasoner_id)

    decision_id = decision_id or str(uuid.uuid4())
    idempotency_key = idempotency_key or observation.event_id
    latency: dict[str, float] = dict(upstream_latency or {})

    gate_input: GateInput | None = None
    gate_output: GateOutput | None = None
    if routes_to_gate:
        assert composed is not None  # guaranteed by the composition check above
        request = GateRequest(
            decision_id=decision_id,
            observation=observation,
            gate_context=gate_context,
            budget_ms=gate_budget_ms,
        )
        t0 = time.perf_counter()
        gate_output = await composed.gate.evaluate(request)
        latency["gate_ms"] = duration_ms(t0)
        _check_gate_output(composed, gate_output)
        gate_input = GateInput(gate_id=composed.gate_id)
    else:
        latency["gate_ms"] = 0.0

    # --- Enforcement ---
    t0 = time.perf_counter()
    enforcement = resolve(
        observation,
        gate_output,
        decision_id=decision_id,
        registration=registration,
    )
    latency["enforcement_ms"] = duration_ms(t0)

    # --- Bundle assembly + persistence ---
    # Note: bundle assembly + write latency is intentionally not tracked —
    # the bundle is frozen at build_bundle() time, so any post-construction
    # measurement cannot land in the persisted record. Keeping the
    # response total and the persisted total identical is more important
    # than capturing bundle write time.
    bundle = build_bundle(
        decision_id=decision_id,
        obs=observation,
        idempotency_key=idempotency_key,
        ingestion_timestamp=ingestion_timestamp,
        gate_input=gate_input,
        gate_output=gate_output,
        enforcement_decision=enforcement,
        latency_breakdown=latency,
    )
    store.write(bundle)

    return bundle


def _validate_gate_config(
    composed: ComposedGate,
    gate_context: GateContext,
    *,
    reasoner_id: str,
) -> None:
    """Validate ``gate_config`` for the gate it names, for this reasoner.

    Two checks, both owned by the gate: its declared ``config_model`` (the
    shape), then — when the built gate is a ``ConfigAuthorizer`` — whether
    this reasoner may send that configuration. Runs on every route (fast
    path included) so a recorded decision always carries a gate context its
    gate could evaluate for shadow evaluation.

    Raises:
        ObservationContractError: The configuration does not satisfy the
            gate's ``config_model``, or the gate refuses it for this
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
            raise ObservationContractError(
                reasoner_id=reasoner_id,
                violation=(
                    f"gate_config for gate {composed.gate_id!r} does not satisfy "
                    f"{config_model.__name__}: {problems}"
                ),
            ) from err

    gate = composed.gate
    if not isinstance(gate, ConfigAuthorizer):
        return
    try:
        gate.authorize_config(config, reasoner_id=reasoner_id)
    except ValueError as err:
        raise ObservationContractError(
            reasoner_id=reasoner_id,
            violation=(
                f"gate_config for gate {composed.gate_id!r} is not authorized "
                f"for this reasoner: {err}"
            ),
        ) from err


def _check_gate_output(composed: ComposedGate, output: object) -> None:
    """Reject a gate output the framework cannot store or replay faithfully.

    The bundle the caller receives must equal the bundle the store reloads,
    so the output and everything typed inside it must be the universal
    contracts themselves: the store deserializes only those, and a subclass's
    extra fields would be silently dropped on the round trip.

    Raises:
        GatePluginError: ``output`` is not exactly ``GateOutput``, its verdict
            is not exactly ``GateVerdict``, an evidence reference is not
            exactly ``EvidenceRef``, it names another gate, or it misstates
            the plugin's ``deterministic`` flag.
    """
    gate_id = composed.gate_id
    if type(output) is not GateOutput:
        raise GatePluginError(
            gate_id,
            f"evaluate() returned {type(output).__name__}; gates return the "
            "universal GateOutput and keep gate-specific fields in "
            "evidence_snapshot / extra (DR-28)",
        )
    if output.verdict is not None and type(output.verdict) is not GateVerdict:
        raise GatePluginError(
            gate_id,
            f"evaluate() returned a {type(output.verdict).__name__} verdict; "
            "the verdict must be the universal GateVerdict (record explanations "
            "in extra)",
        )
    foreign = {
        type(ref).__name__ for ref in output.evidence if type(ref) is not EvidenceRef
    }
    if foreign:
        raise GatePluginError(
            gate_id,
            f"evaluate() returned evidence of type(s) {sorted(foreign)}; "
            "evidence references must be the universal EvidenceRef",
        )
    if output.gate_id != gate_id:
        raise GatePluginError(
            gate_id, f"evaluate() returned a GateOutput for gate {output.gate_id!r}"
        )
    if output.deterministic != composed.plugin.deterministic:
        raise GatePluginError(
            gate_id,
            f"evaluate() returned deterministic={output.deterministic}, but the "
            f"plugin declares deterministic={composed.plugin.deterministic}",
        )
