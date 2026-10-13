"""GateContext — gate dispatch envelope for the DecisionLedger framework.

The domain reasoner builds a GateContext to tell the framework which gate
should process the observation and what configuration that gate needs.

Gate dispatch:
    ``gate_id`` selects the gate (e.g., ``"policy"``). The framework
    validates it against the registration's ``allowed_gate_ids`` and, when
    the observation routes to a gate, dispatches to the gate composed under
    that id (``decisionledger.gates`` entry points, DR-28).

Gate configuration:
    ``gate_config`` is a dict the framework transports to the gate without
    interpreting it. A gate that declares a ``config_model`` has the dict
    validated against that model at intake, so a malformed configuration is
    rejected before anything runs; a gate without one validates on
    consumption. Each gate documents its own keys (the reference policy
    gate's are listed in ``app/gate/policy``). Two keys are still checked
    against the registration by convention, until the policy gate takes
    over that validation: ``template_id`` (against
    ``allowed_prompt_template_ids``) and ``jurisdictions`` (against
    ``allowed_jurisdictions``).

Shadow evaluation:
    GateContext is required on every Observation. Populating it on all
    observations enables shadow evaluation: any decision can be retrospectively
    routed through a decision gate offline to compare scorer and gate outputs on
    the same event.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class GateContext(BaseModel):
    """Gate dispatch envelope — selects the gate and carries its configuration.

    Built by the domain assembler from its own event, feature, and scoring
    artifacts. The framework uses ``gate_id`` for dispatch and authorization,
    then passes ``gate_config`` through to the selected gate implementation.

    Attributes:
        gate_id: Identifier for the gate that should process this
            observation (e.g., ``"policy"``). Validated against the
            registration's ``allowed_gate_ids``.
        gate_config: Gate-specific configuration. Validated against the
            gate's declared ``config_model`` at intake when it has one;
            otherwise interpreted only by the gate. ``None`` means no
            gate-specific config.
    """

    # No dedicated test file — GateContext has no custom validators or
    # cross-field logic. Construction and immutability are exercised by
    # tests/core/observation/test_registration.py via validate_observation().

    model_config = ConfigDict(strict=True, frozen=True)

    gate_id: str
    gate_config: dict[str, Any] | None = None
