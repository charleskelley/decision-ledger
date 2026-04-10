"""Gate layer — contracts for all policy gate types in DecisionLedger.

Defines the routing enum, gate output schema, prompt registry contracts, and
policy corpus metadata. Any gate implementation (policy gate, LLM-as-judge,
etc.) must produce output conforming to these contracts.

Public API:
    GateRouting:      Routing decision from the domain reasoner to the gate.
    Citation:         A single policy citation with policy_id, snippet, and relevance.
    PolicyGateOutput: Structured LLM gate output (Pydantic-validated).
    PromptSnapshot:   Point-in-time snapshot of the prompt template at gate invocation.
    PromptTemplate:   Immutable versioned prompt template contract.
    PromptRegistry:   Protocol for template registries the gate depends on.
    DocumentType:     Document type enum (REGULATION, GUIDANCE, INTERNAL_POLICY,
                      STANDARD).
    PolicyDocument:   Metadata schema for a versioned policy corpus document.
"""

from core.gate.corpus import DocumentType, PolicyDocument
from core.gate.output import Citation, PolicyGateOutput
from core.gate.prompt import PromptRegistry, PromptSnapshot, PromptTemplate
from core.routing import GateRouting

__all__ = [
    "Citation",
    "DocumentType",
    "GateRouting",
    "PolicyDocument",
    "PolicyGateOutput",
    "PromptRegistry",
    "PromptSnapshot",
    "PromptTemplate",
]
