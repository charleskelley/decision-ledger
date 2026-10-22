"""The policy gate's LLM structured-output schemas.

The policy gate asks the LLM for one of two shapes, chosen by the
template's ``evidence_layout`` (DR-34):

* ``PolicyGateVerdict`` (``numbered_list`` templates): the universal
  ``GateVerdict`` fields plus a natural-language ``rationale`` grounded in
  the retrieved policy snippets and typed ``citations`` linking claims to
  sources. Its name, docstring, and fields are the response schema the
  model receives, so they stay exactly as they were: a template written
  before DR-34 gets the request it always got.
* ``TieredPolicyGateVerdict`` (``tiered_blocks`` templates): the same, plus
  the conflict/gap report on evidence grouped by authority tier — which
  corpus governs, whether passages on point disagree, and whether the
  evidence covers the case. When passages disagree or the evidence falls
  short, the gate holds the decision for review.

Both are the gate's private schemas, not bundle types. The gate converts
either into the universal ``GateVerdict`` (what enforcement reads and the
bundle stores) and records ``rationale``, ``citations``, and the report in
its ``PolicyGateExtra`` (``GateOutput.extra``), per DR-28. Audit consumers
— reviewers of HOLDs, faithfulness and citation evaluation — read them
from there.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict

from core.gate.verdict import GateVerdict
from policy_rag_gate.models.citation import (
    Citation,  # noqa: TC001 (runtime Pydantic field)
)


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


class TieredPolicyGateVerdict(PolicyGateVerdict):
    """Structured output for evidence grouped into authority tiers.

    The policy verdict plus a report on the evidence. Passages arrive in
    blocks by tier, tier 1 the highest authority, each labeled with its
    corpus.

    Attributes:
        governing_corpus_id: The corpus whose passage governs the verdict:
            the highest-tier corpus with a passage on point. Null when no
            passage is on point.
        conflict_detected: True when passages on point disagree about what
            the policy requires for this case.
        evidence_sufficient: False when the retrieved passages do not cover
            the case well enough to decide it.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    governing_corpus_id: str | None
    conflict_detected: bool
    evidence_sufficient: bool
