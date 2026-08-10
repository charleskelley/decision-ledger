"""PolicyGateOutput — LLM-backed policy gate's output subclass.

Extends the universal ``GateOutput`` with LLM-specific output artifacts:
the raw text response (forensic evidence when validation fails) and
token-cost billing data. Discriminator value: ``gate_id="policy"``.

How ``verdict`` and ``response_text`` interact — three concrete cases:

  * ``verdict`` is a ``PolicyGateVerdict``, ``response_text`` is the raw
    LLM string: the gate ran and the LLM's JSON validated.
    ``response_text`` enables forensic round-tripping of the model's
    output.
  * ``verdict is None``, ``response_text`` is the raw LLM string: the
    gate ran but the JSON failed schema validation. ``response_text``
    carries the emitted artifact for HOLD-review investigation;
    enforcement routes via the schema-failure tier (DR-19).
  * ``verdict is None``, ``response_text is None``: the LLM call itself
    errored (network failure, timeout, refusal) before producing any
    text. ``token_cost`` is also ``None`` in this case.

The bundle-level *gate not invoked* case (``bundle.gate_output is None``,
e.g. fast path) is a bundle concern, not a ``PolicyGateOutput`` state —
this type only exists when the gate ran. See ``core/gate/output.py`` for
the universal contract.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from core.gate.output import GateOutput
from core.gate.policy.verdict import (
    PolicyGateVerdict,  # noqa: TC001 (runtime Pydantic field)
)


class TokenCost(BaseModel):
    """Token usage and cost breakdown for a single LLM call.

    LLM-specific by definition. Lives under ``core/gate/policy/`` because
    only LLM-backed gates produce token-billing data.

    Attributes:
        prompt_tokens: Number of tokens in the prompt.
        completion_tokens: Number of tokens in the completion.
        total_tokens: Total token count (prompt + completion).
        cost_usd: Estimated cost in US dollars at the time of inference.
        model: Model identifier as returned by the API
            (e.g., ``"gpt-4o-2024-08-06"``).
    """

    model_config = ConfigDict(strict=True, frozen=True)

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)
    model: str


class PolicyGateOutput(GateOutput):
    """Output from the LLM-backed policy gate.

    Attributes:
        gate_id: Discriminator value ``"policy"``.
        verdict: ``PolicyGateVerdict`` (narrowed from the universal
            ``GateVerdict``) when validation succeeded; ``None`` when the
            LLM response failed schema validation.
        response_text: Raw LLM string output, pre-validation. Carries the
            forensic evidence that backs ``verdict``; populated for both
            success and validation-failure cases. ``None`` only when the
            LLM call itself errored before producing text.
        token_cost: Token usage and cost. Populated when the LLM call
            completed; ``None`` on API errors before completion.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    gate_id: Literal["policy"] = "policy"
    verdict: PolicyGateVerdict | None
    response_text: str | None = None
    token_cost: TokenCost | None = None
