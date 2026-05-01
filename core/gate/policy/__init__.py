"""Policy gate — concrete contracts and helpers for the LLM-backed policy gate.

Houses every type that's specific to the LLM-backed policy gate
implementation: typed input/output/verdict subclasses, citations, prompt
template metadata, and the prompt-registry protocol. Universal contracts
(``GateInput``, ``GateOutput``, ``GateVerdict``) live one level up, in
``core.gate``.

Adding a new gate kind: create a sibling subpackage (e.g.,
``core/gate/rule/``) following the same pattern — concrete subclasses of
the universal contracts plus any kind-specific helpers.
"""

from core.gate.policy.citation import Citation
from core.gate.policy.input import PolicyGateInput
from core.gate.policy.output import PolicyGateOutput, TokenCost
from core.gate.policy.prompt import PromptRegistry, PromptSnapshot, PromptTemplate
from core.gate.policy.verdict import PolicyGateVerdict

__all__ = [
    "Citation",
    "PolicyGateInput",
    "PolicyGateOutput",
    "PolicyGateVerdict",
    "PromptRegistry",
    "PromptSnapshot",
    "PromptTemplate",
    "TokenCost",
]
