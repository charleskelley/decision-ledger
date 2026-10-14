"""The RAG policy gate's own typed models.

Houses every type that is specific to the policy gate: its structured-output
verdict schema (``PolicyGateVerdict``), citations, the corpus document
schema, prompt template metadata and the prompt-registry protocol, the
retrieved snippet, and the schemas of the opaque extension it writes into
the universal ``GateOutput`` (``PolicyGateSnapshot`` for
``evidence_snapshot``, ``PolicyGateExtra`` for ``extra``). The universal
contracts (``GateInput``, ``GateOutput``, ``GateVerdict``, ``EvidenceRef``)
live in the framework's ``core.gate``; the framework stores only those and
never imports these (DR-28).

These models are the gate's private vocabulary. Readers that know this gate
(its eval, a reasoner's baseline capture) re-validate a stored bundle's
``evidence_snapshot`` / ``extra`` with them.
"""

from policy_rag_gate.models.citation import Citation
from policy_rag_gate.models.output import PolicyGateExtra, PolicyGateFailure, TokenCost
from policy_rag_gate.models.prompt import PromptRegistry, PromptSnapshot, PromptTemplate
from policy_rag_gate.models.snapshot import PolicyGateSnapshot
from policy_rag_gate.models.verdict import PolicyGateVerdict

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
