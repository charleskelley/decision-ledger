"""Policy gate output extension — ``TokenCost`` and ``PolicyGateExtra``.

The LLM-backed policy gate returns the universal ``GateOutput`` (DR-28).
Everything it produces beyond the universal verdict goes into
``GateOutput.extra`` as a ``PolicyGateExtra`` serialized with
``model_dump(mode="json")``: the model's rationale and citations, the raw
response text, token cost, the gate's internal retrieval latency, and, when
there is no verdict, which stage failed. The framework stores it verbatim and
never reads it; readers that know this gate re-validate it with
``PolicyGateExtra.model_validate(...)``.

How ``verdict`` and the extension interact — three concrete cases:

  * ``verdict`` present: the LLM's structured output validated.
    ``rationale`` and ``citations`` carry its explanation and
    ``token_cost`` is populated.
  * ``verdict is None``, ``failure`` is ``"missing_retrieval_query"`` or
    ``"render_error"``: the gate stopped before calling the LLM;
    ``token_cost`` is ``None``.
  * ``verdict is None``, ``failure == "llm_error"``: the LLM call errored or
    its output failed schema validation at the SDK boundary;
    ``token_cost`` is ``None``.

Enforcement routes every ``verdict is None`` case to HOLD through the
schema-failure tier (DR-19).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from policy_rag_gate.models.citation import Citation

#: Stage at which the policy gate failed to produce a verdict.
PolicyGateFailure = Literal["missing_retrieval_query", "render_error", "llm_error"]


class TokenCost(BaseModel):
    """Token usage and cost breakdown for a single LLM call.

    LLM-specific by definition: only LLM-backed gates produce
    token-billing data.

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


class PolicyGateExtra(BaseModel):
    """Schema of the policy gate's ``GateOutput.extra``.

    Attributes:
        rationale: The model's plain-language reasoning grounded in the
            retrieved evidence. ``None`` when there is no verdict.
        citations: Policy citations supporting the rationale. Empty when
            there is no verdict or the model cited nothing.
        response_text: Raw LLM output, pre-validation, when the client
            surfaces it. The strict structured-output client does not, so
            this is ``None`` in the reference deployment.
        token_cost: Token usage and cost. ``None`` when the LLM call did not
            complete.
        retrieval_ms: Time the gate spent in hybrid retrieval, in
            milliseconds (part of the bundle's ``gate_ms``).
        failure: Stage at which the gate failed to produce a verdict;
            ``None`` when a verdict is present.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    rationale: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    response_text: str | None = None
    token_cost: TokenCost | None = None
    retrieval_ms: float = Field(default=0.0, ge=0.0)
    failure: PolicyGateFailure | None = None
