"""GateContext — gate dispatch envelope for the DecisionLedger framework.

A submission carries one ``GateContext`` per gate in the reasoner's gate
set: it addresses that gate and carries the configuration the gate needs.

Gate dispatch:
    ``gate_id`` names the gate (e.g., ``"policy"``). The framework checks it
    against the gates the reasoner's registration binds (its
    ``GateBinding``s; a gate the deployment mandates without a binding runs
    with an empty configuration and cannot be addressed) and, when the gate
    runs, dispatches to the gate composed under that id
    (``decisionledger.gates`` entry points, DR-28).

Gate configuration:
    ``gate_config`` is a dict the framework transports to the gate without
    interpreting it. A gate that declares a ``config_model`` has the dict
    validated against that model at intake, so a malformed configuration is
    rejected before anything runs; a gate without one validates on
    consumption. A gate may also authorize the configuration per submitting
    reasoner at intake (for example, only resources that reasoner ships).
    Each gate documents its own keys in its own package (DR-28); the
    framework reads none of them.

Shadow evaluation:
    Every bound gate is addressed on every submission — recorded ones
    included, although a record runs only the mandatory gates. That lets any
    recorded decision be evaluated by its advisory gates offline, comparing
    the reasoner's decision with what the gates would have said on the same
    event (the eval harness's shadow dimension).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, JsonValue


class GateContext(BaseModel):
    """Gate dispatch envelope — selects the gate and carries its configuration.

    Built by the reasoner from its own event, feature, and scoring
    artifacts. The framework uses ``gate_id`` for dispatch and authorization,
    then passes ``gate_config`` through to the selected gate implementation.

    Attributes:
        gate_id: Identifier of the gate this envelope addresses (e.g.,
            ``"policy"``). Must name a gate in the reasoner's gate set.
        gate_config: Gate-specific configuration, as JSON values (it is
            stored verbatim in the bundle and must survive the round trip).
            Validated against the gate's declared ``config_model`` at intake
            when it has one; otherwise interpreted only by the gate.
            ``None`` means no gate-specific config.
    """

    # No dedicated test file — GateContext has no custom validators or
    # cross-field logic. Construction and immutability are exercised by
    # tests/core/test_submission.py.

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", allow_inf_nan=False
    )

    gate_id: str
    gate_config: dict[str, JsonValue] | None = None
