"""GateVerdict — universal, enforcement-consumable verdict contract.

Every gate produces a ``GateVerdict`` that the framework's deterministic
enforcement layer reads to produce a ``decision_action``. Per DR-20 this
contract is genuinely universal: fields are limited to those enforcement
actually consumes.

Gate-specific explanation artifacts (an LLM rationale and its citations, a
rule-engine trace, model feature contributions) are not verdict fields. A
gate records them in its ``GateOutput.extra`` / ``evidence_snapshot``
(DR-28), and the bundle stores exactly this universal verdict. A gate may
use a richer model internally (for example as an LLM structured-output
schema) as long as what it returns is this base type.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from core.actions import DecisionAction


class GateVerdict(BaseModel):
    """Universal, enforcement-consumable verdict.

    Carries only the fields ``app.enforcement.resolver`` reads.
    Explanation artifacts the framework does not interpret live in the
    gate's ``GateOutput`` extension, not here.

    Attributes:
        gate_id: Identifier of the gate that produced this verdict. Equals
            the enclosing ``GateOutput.gate_id``.
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
