"""The RAG policy gate's own typed models.

Houses every type that is specific to the policy gate: its structured-output
verdict schemas (``PolicyGateVerdict``, and ``TieredPolicyGateVerdict`` for
templates that render evidence by authority tier), citations, the corpus
document schema, prompt template metadata and the prompt-registry protocol,
the retrieved passage (``RetrievedContext``, the evidence contract every
retrieval provider meets, DR-34), and the schemas of the opaque extension it
writes into the universal ``GateOutput`` (``PolicyGateSnapshot`` with its
per-corpus ``CorpusQuery`` records for ``evidence_snapshot``,
``PolicyGateExtra`` for ``extra``). The universal contracts (``GateInput``,
``GateOutput``, ``GateVerdict``, ``EvidenceRef``) live in the framework's
``core.gate``; the framework stores only those and never imports these
(DR-28). Corpus references and binding configuration live in
``policy_rag_gate.corpus``.

These models are the gate's private vocabulary. Readers that know this gate
(its eval, a reasoner's baseline capture) re-validate a stored bundle's
``evidence_snapshot`` / ``extra`` with them.
"""

from policy_rag_gate.models.citation import Citation
from policy_rag_gate.models.context import RetrievedContext, content_hash
from policy_rag_gate.models.output import (
    PolicyGateExtra,
    PolicyGateFailure,
    ProtocolHold,
    TokenCost,
)
from policy_rag_gate.models.prompt import (
    EvidenceLayout,
    PromptRegistry,
    PromptSnapshot,
    PromptTemplate,
)
from policy_rag_gate.models.snapshot import (
    CorpusQuery,
    PolicyGateSnapshot,
    RetrievalFilters,
)
from policy_rag_gate.models.verdict import PolicyGateVerdict, TieredPolicyGateVerdict

__all__ = [
    "Citation",
    "CorpusQuery",
    "EvidenceLayout",
    "PolicyGateExtra",
    "PolicyGateFailure",
    "PolicyGateSnapshot",
    "PolicyGateVerdict",
    "PromptRegistry",
    "PromptSnapshot",
    "PromptTemplate",
    "ProtocolHold",
    "RetrievalFilters",
    "RetrievedContext",
    "TieredPolicyGateVerdict",
    "TokenCost",
    "content_hash",
]
