"""Policy gate output contracts and citation schema."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from core.actions import DecisionAction


class Citation(BaseModel):
    """A single policy citation supporting a rationale claim.

    Args:
        policy_id: Identifier of the cited policy document.
        snippet: The specific text passage cited (verbatim from the corpus).
        relevance: Human-readable explanation of how this snippet supports
            the rationale claim it is attached to.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    policy_id: str
    snippet: str
    relevance: str


class PolicyGateOutput(BaseModel):
    """Structured LLM policy gate output, validated against this Pydantic schema.

    The policy gate produces this record as structured JSON. Pydantic validation
    is the enforcement boundary — a response that fails validation is logged as
    ``raw_llm_response`` and the event is routed to ``HOLD``. A passing
    ``PolicyGateOutput`` is never modified by the enforcement layer; enforcement
    only selects from ``permitted_actions``.

    Args:
        permitted_actions: Actions the policy permits for this event. Enforcement
            selects the most conservative action from this list.
        required_controls: Controls that must be applied alongside the action
            (e.g., "log_step_up_reason", "notify_account_owner").
        rationale: Plain-language reasoning grounded in retrieved policy evidence.
            Maximum 200 words.
        citations: Policy citations supporting the rationale. Each citation links
            a specific snippet to its claim.
        confidence: Gate's self-reported confidence in the output, in [0.0, 1.0].
            Low confidence + high-risk action triggers a HOLD override in enforcement.
        escalate_to_human: True if the gate recommends human review regardless
            of the final action.
        escalation_reason: Required when ``escalate_to_human`` is True; describes
            the specific concern requiring human judgment.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    permitted_actions: list[DecisionAction]
    required_controls: list[str]
    rationale: str
    citations: list[Citation]
    confidence: float = Field(ge=0.0, le=1.0)
    escalate_to_human: bool
    escalation_reason: str | None
