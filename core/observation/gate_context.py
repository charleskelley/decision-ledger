"""GateContext — gate dispatch envelope for the DecisionLedger framework.

The domain reasoner builds a GateContext to tell the framework which gate
should process the observation and what configuration that gate needs.

Gate dispatch:
    ``gate_id`` selects the gate implementation (e.g., ``"policy"``). The
    framework validates this against the registration's ``allowed_gate_ids``
    before routing.

Gate configuration:
    ``gate_config`` is an opaque dict the framework transports without
    interpretation. The selected gate implementation validates and extracts
    what it needs on consumption. Well-known keys used by the reference
    policy gate:

    - ``template_id`` (``str``) — Versioned prompt template identifier,
      used as the ``PromptRegistry`` lookup key. Validated by the framework
      against ``allowed_prompt_template_ids`` when present.
    - ``template_vars`` (``dict[str, str]``) — Domain-rendered substitutions
      for template placeholders.
    - ``jurisdictions`` (``list[str]``) — Retrieval filter for regulatory
      jurisdiction codes. Validated by the framework against
      ``allowed_jurisdictions`` when present.
    - ``risk_tier`` (``str``) — Retrieval filter applied to pgvector only.
    - ``target_model`` (``str``) — LLM model identifier hint.

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

    Args:
        gate_id: Identifier for the gate that should process this
            observation (e.g., ``"policy"``). Validated against the
            registration's ``allowed_gate_ids``.
        gate_config: Gate-type-specific configuration the framework
            transports opaquely. The gate implementation validates and
            interprets this dict. ``None`` means no gate-specific config.
            See module docstring for well-known keys.
    """

    # No dedicated test file — GateContext has no custom validators or
    # cross-field logic. Construction and immutability are exercised by
    # tests/core/observation/test_registration.py via validate_observation().

    model_config = ConfigDict(strict=True, frozen=True)

    gate_id: str
    gate_config: dict[str, Any] | None = None
