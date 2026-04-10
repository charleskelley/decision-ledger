"""Framework Observation protocol and intake validation.

Domain reasoner implementations (e.g., LoginEvent in reasoner/account_takeover/)
satisfy the Observation protocol structurally (PEP 544). No inheritance is
required or expected.

An Observation is what the domain reasoner **produces** before handing control
to the DecisionLedger framework — not the raw event that enters the domain
pipeline. The domain reasoner is responsible for running its own feature
computation, scoring, and context assembly, then packaging the result as an
Observation with routing, reasoner_context, gate_context, and (on fast paths)
fast_path_rationale populated.

validate_observation() is the framework's intake gate. It must be called before
any routing or policy gate invocation. Shape violations raise
ObservationContractError; unrecognized reasoners raise UnregisteredReasonerError.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from core.exceptions import ObservationContractError, UnregisteredReasonerError
from core.routing import GateRouting

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from core.observation.gate_context import GateContext
    from core.observation.reasoner_context import ReasonerContext
    from core.observation.registration import ReasonerRegistry


@runtime_checkable
class Observation(Protocol):
    """Minimum contract for any event submitted to the DecisionLedger framework.

    Any type that provides these fields satisfies this protocol. Domain
    reasoner implementations are not required to inherit from Observation —
    structural subtyping (PEP 544) applies.

    The domain reasoner populates all framework fields (``routing``,
    ``reasoner_context``, ``gate_context``, and ``fast_path_rationale`` on
    fast-path observations) from its own scorer and feature artifacts before
    submitting to the framework. The framework never reads domain-specific
    field names.

    Attributes:
        event_id: Unique identifier for this event instance.
        entity_id: Framework-level UUID identifying the subject of the
            decision. Derived deterministically from the domain business key
            (e.g., via UUID5 from account_id).
        entity_type: Classification of the subject
            (e.g., ``"account"``, ``"customer"``, ``"vendor"``).
        timestamp: When the event occurred (UTC).
        routing: Routing decision produced by the domain reasoner. Determines
            whether the observation takes the fast path or is forwarded to
            the policy gate.
        reasoner_context: Generic evidence record from the domain reasoner.
            Contains model version, prediction, feature snapshot, and
            attribution. Required in governed mode — the framework stores
            this verbatim in the DecisionBundle for audit and replay.
            ``None`` in intermediate pipeline stages before the assembler
            has run; the framework raises if it is ``None`` at submission.
        fast_path_rationale: Required when ``routing`` is ``FAST_PATH_ALLOW``
            or ``FAST_PATH_BLOCK``. A human-readable explanation of why the
            policy gate was bypassed (e.g., the confidence-band rule that
            fired). ``None`` when ``routing`` is ``ROUTE_TO_GATE``. The
            framework raises ``ObservationContractError`` if this is absent
            on a fast-path observation.
        gate_context: Domain-assembled policy gate context. Required on every
            observation — including fast-path ones — to enable shadow
            evaluation: any fast-path decision can be retrospectively sent
            through the gate offline to compare scorer and gate outputs
            without re-running the domain pipeline.
    """

    event_id: str
    entity_id: UUID
    entity_type: str
    timestamp: datetime
    routing: GateRouting
    reasoner_context: ReasonerContext | None
    fast_path_rationale: str | None
    gate_context: GateContext


def validate_observation(
    obs: Observation,
    *,
    registry: ReasonerRegistry | None = None,
) -> None:
    """Validate that an Observation satisfies all DecisionLedger intake contracts.

    Called by the framework at the point of submission, before any routing or
    policy gate invocation. Shape checks run first; registry authorization
    checks run after, if a registry is provided.

    Validated contracts (shape):

    - ``reasoner_context`` must not be ``None`` in governed mode.
    - ``fast_path_rationale`` must be present when ``routing`` is a fast-path
      value — the policy gate was bypassed and the rationale is the audit trail.

    Validated contracts (registry, when ``registry`` is provided):

    - ``reasoner_context.reasoner_id`` must be registered.
    - ``gate_context.prompt_template_id`` must be in the registration's
      ``allowed_prompt_template_ids``.
    - Every entry in ``gate_context.jurisdictions`` must be in the
      registration's ``allowed_jurisdictions`` (skipped when either is empty).

    Args:
        obs: The Observation submitted by the domain assembler.
        registry: Optional registry to validate reasoner authorization.
            When ``None``, only shape contracts are checked.

    Raises:
        ObservationContractError: If a shape or authorization contract
            is violated.
        UnregisteredReasonerError: If ``registry`` is provided and
            ``obs.reasoner_context.reasoner_id`` is not registered.
    """
    # --- Shape checks ---
    if obs.reasoner_context is None:
        raise ObservationContractError(
            reasoner_id="unknown",
            violation=(
                "reasoner_context is None — all governed-mode Observations must "
                "carry a fully-populated ReasonerContext before framework submission"
            ),
        )
    if (
        obs.routing in (GateRouting.FAST_PATH_ALLOW, GateRouting.FAST_PATH_BLOCK)
        and not obs.fast_path_rationale
    ):
        raise ObservationContractError(
            reasoner_id=obs.reasoner_context.reasoner_id,
            violation=(
                f"fast_path_rationale is required when routing={obs.routing.value} "
                "— the policy gate was bypassed without a documented rationale, "
                "violating the DecisionLedger replayability contract"
            ),
        )

    # --- Registry authorization checks ---
    if registry is None:
        return

    reasoner_id = obs.reasoner_context.reasoner_id

    if not registry.is_registered(reasoner_id):
        raise UnregisteredReasonerError(reasoner_id)

    registration = registry.get(reasoner_id)

    template_id = obs.gate_context.prompt_template_id
    if template_id not in registration.allowed_prompt_template_ids:
        raise ObservationContractError(
            reasoner_id=reasoner_id,
            violation=(
                f"prompt_template_id '{template_id}' is not in the allowed set "
                f"for this reasoner: {sorted(registration.allowed_prompt_template_ids)}"
            ),
        )

    if registration.allowed_jurisdictions and obs.gate_context.jurisdictions:
        disallowed = (
            set(obs.gate_context.jurisdictions) - registration.allowed_jurisdictions
        )
        if disallowed:
            raise ObservationContractError(
                reasoner_id=reasoner_id,
                violation=(
                    "gate_context.jurisdictions contains jurisdiction(s) not "
                    f"registered for this reasoner: {sorted(disallowed)}"
                ),
            )
