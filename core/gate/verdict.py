"""GateVerdict — universal, enforcement-consumable verdict contract.

Every gate implementation produces a ``GateVerdict`` (or a subclass) that
the framework's deterministic enforcement layer reads to produce a
``decision_action``. Per DR-20 this contract is genuinely universal:
fields are limited to those enforcement actually consumes.

Gate-implementation-specific explanation artifacts (LLM rationale + policy
citations, rule-engine triggered-rule trace, ML feature contributions,
etc.) live on per-gate-type subclasses in their own subpackage — see
``core/gate/policy/verdict.py`` for the reference LLM policy gate's
``PolicyGateVerdict``.

Discrimination across subclasses is by ``gate_id`` (Pydantic discriminator
narrowed to ``Literal[...]`` in each subclass).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from core.actions import DecisionAction


class GateVerdict(BaseModel):
    """Universal, enforcement-consumable verdict.

    Carries only the fields ``app.enforcement.resolver.resolve()`` reads.
    Concrete gate-type subclasses (e.g., ``PolicyGateVerdict``) extend
    with explanation artifacts the framework does not interpret.

    Args:
        gate_id: Identifier of the gate that produced this verdict.
            Subclasses narrow this to ``Literal[...]`` for Pydantic
            discriminated-union deserialization.
        permitted_actions: Actions the gate permits for this event.
            Enforcement selects the most conservative action from this list.
        required_controls: Controls that must be applied alongside the
            action (e.g., ``"log_step_up_reason"``,
            ``"adversarial_probe_detected"``,
            ``"jurisdiction_conflict"``). Enforcement matches against
            sentinel values.
        confidence: Gate's self-reported confidence in the verdict, in
            [0.0, 1.0]. Low confidence + high-risk action triggers a HOLD
            override in enforcement.
        escalate_to_human: True if the gate recommends human review
            regardless of the chosen action.
        escalation_reason: Required when ``escalate_to_human`` is True;
            describes the specific concern requiring human judgment.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    gate_id: str
    permitted_actions: list[DecisionAction]
    required_controls: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    escalate_to_human: bool
    escalation_reason: str | None
