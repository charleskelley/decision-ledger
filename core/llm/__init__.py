"""Universal LLM-call abstraction.

Single SDK-agnostic primitive (``LLMClient``) for any component that calls
a model — an LLM-backed gate, an eval judge. The framework ships the
protocol only; concrete adapters (one per provider) live with the gate that
uses them, in its workspace member, and are wired at that gate's
construction edge (DR-23, DR-28).

See DR-23 in ``docs/design/decisions.md`` for the rationale.
"""

from core.llm.client import CompletionResult, LLMClient, TokenUsage

__all__ = ["CompletionResult", "LLMClient", "TokenUsage"]
