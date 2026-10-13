"""Policy gate — the reference LLM-backed policy gate's own typed models.

Houses every type that is specific to the policy gate: its structured-output
verdict schema (``PolicyGateVerdict``), citations, prompt template metadata
and the prompt-registry protocol, and the schemas of the opaque extension it
writes into the universal ``GateOutput`` (``PolicyGateSnapshot`` for
``evidence_snapshot``, ``PolicyGateExtra`` for ``extra``). Universal
contracts (``GateInput``, ``GateOutput``, ``GateVerdict``) live one level
up, in ``core.gate``; the framework stores only those (DR-28).

These models are the gate's private vocabulary. They stay here while the
gate's runtime lives in ``app/gate/policy`` and move with it into its own
workspace member in Phase 1b.
"""

from core.gate.policy.citation import Citation
from core.gate.policy.output import PolicyGateExtra, PolicyGateFailure, TokenCost
from core.gate.policy.prompt import PromptRegistry, PromptSnapshot, PromptTemplate
from core.gate.policy.snapshot import PolicyGateSnapshot
from core.gate.policy.verdict import PolicyGateVerdict

__all__ = [
    "Citation",
    "PolicyGateExtra",
    "PolicyGateFailure",
    "PolicyGateSnapshot",
    "PolicyGateVerdict",
    "PromptRegistry",
    "PromptSnapshot",
    "PromptTemplate",
    "TokenCost",
]
