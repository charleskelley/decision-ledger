"""Framework Observation protocol and intake validation.

Domain reasoner implementations satisfy the Observation protocol structurally
(PEP 544). No inheritance is required or expected.

An Observation is what the domain reasoner **produces** before handing control
to the DecisionLedger framework — not the raw event that enters the domain
pipeline. The domain reasoner is responsible for its own feature computation,
model inference, and context assembly, then packaging the result as an
Observation with routing, reasoner_context, gate_context, and (on fast paths)
fast_path_rationale populated.

validate_observation() is the framework's intake gate. It must be called before
any routing or gate invocation. Shape violations raise
ObservationContractError; unrecognized reasoners raise UnregisteredReasonerError.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from core.exceptions import ObservationContractError, UnregisteredReasonerError
from core.routes import GateRoute

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from core.observation.gate_context import GateContext
    from core.observation.reasoner_context import ReasonerContext
    from core.observation.registration import ReasonerRegistration, ReasonerRegistry


@runtime_checkable
class Observation(Protocol):
    """Minimum contract for any event submitted to the DecisionLedger framework.

    Any type that provides these fields satisfies this protocol. Domain
    reasoner implementations are not required to inherit from Observation —
    structural subtyping (PEP 544) applies.

    The domain reasoner populates all framework fields (``routing``,
    ``reasoner_context``, ``gate_context``, and ``fast_path_rationale`` on
    fast-path observations) before submitting to the framework. The
    framework never reads domain-specific field names.

    Attributes:
        event_id: Domain-assigned unique identifier for the raw event
            (e.g., a login event ID, transaction ID, or claim ID).
            Must be unique per event instance. Used by the framework
            for deduplication, bundle idempotency, structured log
            correlation, and feature-store windowing. Distinct from
            ``entity_id`` (the subject) and ``decision_id`` (the
            framework-generated decision record).
        entity_id: Framework-level UUID identifying the subject of the
            decision. Derived deterministically from the domain business key
            (e.g., via UUID5 from an account or customer identifier).
        entity_type: Classification of the subject
            (e.g., ``"account"``, ``"customer"``, ``"vendor"``).
        timestamp: When the event occurred (UTC).
        route: Gate route selected by the domain reasoner. Determines
            whether the observation takes the fast path or is forwarded to
            a decision gate.
        reasoner_context: Generic evidence record from the domain reasoner.
            Contains model version, prediction, feature snapshot, and
            attribution. Required in governed mode — the framework stores
            this verbatim in the DecisionBundle for audit and replay.
            ``None`` in intermediate pipeline stages before the assembler
            has run; the framework raises if it is ``None`` at submission.
        fast_path_rationale: Required when ``routing`` is ``FAST_PATH_ALLOW``
            or ``FAST_PATH_BLOCK``. A human-readable explanation of why the
            gate was bypassed (e.g., the confidence-band rule that fired).
            ``None`` when ``routing`` is ``ROUTE_TO_GATE``. The framework
            raises ``ObservationContractError`` if this is absent on a
            fast-path observation.
        gate_context: Gate dispatch envelope. Required on every observation
            — including fast-path ones — to enable shadow evaluation: any
            fast-path decision can be retrospectively sent through a gate
            offline to compare reasoner and gate outputs without re-running
            the domain pipeline.
    """

    event_id: str
    entity_id: UUID
    entity_type: str
    timestamp: datetime
    route: GateRoute
    reasoner_context: ReasonerContext | None
    fast_path_rationale: str | None
    gate_context: GateContext


def validate_observation(
    observation: Observation,
    *,
    registry: ReasonerRegistry | None = None,
) -> None:
    """Validate that an Observation satisfies all DecisionLedger intake contracts.

    Called by the framework at the point of submission, before any gate
    invocation. Shape checks run unconditionally; registry authorization
    checks run only when a registry is provided.

    Args:
        observation: The Observation submitted by the domain assembler.
        registry: Optional registry to validate reasoner authorization.
            When ``None``, only shape contracts are checked.

    Raises:
        ObservationContractError: If a shape or authorization contract
            is violated.
        UnregisteredReasonerError: If ``registry`` is provided and the
            reasoner is not registered.
    """
    _validate_reasoner_context_present(observation)
    _validate_fast_path_rationale(observation)
    if registry is None:
        return
    registration = _validate_reasoner_registered(observation, registry)
    _validate_gate_authorization(observation, registration)
    _validate_gate_config_conventions(observation, registration)


# ---------------------------------------------------------------------------
# Shape checks — run unconditionally
# ---------------------------------------------------------------------------


def _validate_reasoner_context_present(observation: Observation) -> None:
    """Governed-mode observations must carry a populated ReasonerContext."""
    if observation.reasoner_context is None:
        raise ObservationContractError(
            reasoner_id="unknown",
            violation=(
                "reasoner_context is None — all governed-mode Observations must "
                "carry a fully-populated ReasonerContext before framework submission"
            ),
        )


def _validate_fast_path_rationale(observation: Observation) -> None:
    """Fast-path observations must document why the gate was bypassed."""
    if (
        observation.route in (GateRoute.FAST_PATH_ALLOW, GateRoute.FAST_PATH_BLOCK)
        and not observation.fast_path_rationale
    ):
        assert observation.reasoner_context is not None  # guaranteed by prior check

        raise ObservationContractError(
            reasoner_id=observation.reasoner_context.reasoner_id,
            violation=(
                f"fast_path_rationale is required when route={observation.route.value} "
                "— the gate was bypassed without a documented rationale, "
                "violating the DecisionLedger replayability contract"
            ),
        )


# ---------------------------------------------------------------------------
# Registry authorization checks — run when a registry is provided
# ---------------------------------------------------------------------------


def _validate_reasoner_registered(
    observation: Observation,
    registry: ReasonerRegistry,
) -> ReasonerRegistration:
    """Verify the reasoner is registered and return its registration."""
    assert observation.reasoner_context is not None  # guaranteed by prior check

    reasoner_id = observation.reasoner_context.reasoner_id
    if not registry.is_registered(reasoner_id):
        raise UnregisteredReasonerError(reasoner_id)

    return registry.get(reasoner_id)


def _validate_gate_authorization(
    observation: Observation,
    registration: ReasonerRegistration,
) -> None:
    """Verify the observation's gate_id is allowed for this reasoner."""
    assert observation.reasoner_context is not None

    gate_id = observation.gate_context.gate_id
    if gate_id not in registration.allowed_gate_ids:
        raise ObservationContractError(
            reasoner_id=registration.reasoner_id,
            violation=(
                f"gate_id '{gate_id}' is not in the allowed set "
                f"for this reasoner: {sorted(registration.allowed_gate_ids)}"
            ),
        )


def _validate_gate_config_conventions(
    observation: Observation,
    registration: ReasonerRegistration,
) -> None:
    """Check well-known gate_config keys against registration constraints.

    Convention-based: skipped when the key is absent from gate_config.
    Checks template_id against allowed_prompt_template_ids and
    jurisdictions against allowed_jurisdictions.
    """
    gate_config = observation.gate_context.gate_config or {}

    template_id = gate_config.get("template_id")
    if (
        template_id is not None
        and registration.allowed_prompt_template_ids
        and template_id not in registration.allowed_prompt_template_ids
    ):
        raise ObservationContractError(
            reasoner_id=registration.reasoner_id,
            violation=(
                f"gate_config template_id '{template_id}' is not in the allowed "
                f"set for this reasoner: "
                f"{sorted(registration.allowed_prompt_template_ids)}"
            ),
        )

    claimed_jurisdictions = gate_config.get("jurisdictions", [])
    if registration.allowed_jurisdictions and claimed_jurisdictions:
        disallowed = set(claimed_jurisdictions) - registration.allowed_jurisdictions

        if disallowed:
            raise ObservationContractError(
                reasoner_id=registration.reasoner_id,
                violation=(
                    "gate_config jurisdictions contains jurisdiction(s) not "
                    f"registered for this reasoner: {sorted(disallowed)}"
                ),
            )
