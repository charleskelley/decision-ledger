"""PolicyGateVerdict — LLM-backed policy gate's verdict subclass.

Extends the universal ``GateVerdict`` with explanation artifacts the
policy gate produces: natural-language rationale grounded in retrieved
policy snippets, plus typed citations linking each claim to its source.

The framework's enforcement layer reads only the universal fields
(``permitted_actions``, ``required_controls``, ``confidence``,
``escalate_to_human``, ``escalation_reason``). ``rationale`` and
``citations`` are audit-side artifacts surfaced to humans reviewing
HOLDs, debugging schema-failures, or tracing decisions.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict

from core.gate.policy.citation import Citation  # noqa: TC001 (runtime Pydantic field)
from core.gate.verdict import GateVerdict


class PolicyGateVerdict(GateVerdict):
    """Verdict from the LLM-backed policy gate.

    Adds rationale and citations to the universal verdict. The framework
    does not interpret these fields; they are surfaced to audit consumers.

    Attributes:
        gate_id: Discriminator value ``"policy"``. Pydantic ``Literal``
            narrowing of the universal ``GateVerdict.gate_id`` field —
            drives discriminated-union deserialization at the bundle
            layer (``app/audit/store.py``).
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
