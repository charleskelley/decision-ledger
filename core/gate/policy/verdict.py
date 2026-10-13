"""PolicyGateVerdict — the policy gate's LLM structured-output schema.

The policy gate asks the LLM for this shape: the universal ``GateVerdict``
fields plus a natural-language ``rationale`` grounded in the retrieved
policy snippets and typed ``citations`` linking claims to sources.

It is the gate's private schema, not a bundle type. The gate converts it into
the universal ``GateVerdict`` (what enforcement reads and the bundle stores)
and records ``rationale`` and ``citations`` in its ``PolicyGateExtra``
(``GateOutput.extra``), per DR-28. Audit consumers — reviewers of HOLDs,
faithfulness and citation evaluation — read them from there.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict

from core.gate.policy.citation import Citation  # noqa: TC001 (runtime Pydantic field)
from core.gate.verdict import GateVerdict


class PolicyGateVerdict(GateVerdict):
    """Structured output the policy gate requests from the LLM.

    Attributes:
        gate_id: Always ``"policy"`` — the policy gate's identifier.
        rationale: Plain-language reasoning grounded in retrieved policy
            evidence. Maximum 200 words by convention.
        citations: Policy citations supporting the rationale. Each
            citation links a specific snippet to its claim. Empty list
            permitted when the gate's reasoning didn't reference specific
            corpus passages.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    gate_id: Literal["policy"] = "policy"
    rationale: str
    citations: list[Citation]

    def universal(self) -> GateVerdict:
        """Return the universal ``GateVerdict`` part of this verdict.

        Returns:
            A plain ``GateVerdict`` with the same enforcement fields — the
            only verdict type the bundle stores.
        """
        return GateVerdict(
            gate_id=self.gate_id,
            permitted_actions=list(self.permitted_actions),
            required_controls=list(self.required_controls),
            confidence=self.confidence,
            escalate_to_human=self.escalate_to_human,
            escalation_reason=self.escalation_reason,
        )
