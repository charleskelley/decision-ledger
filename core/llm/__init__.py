"""Universal LLM-call abstraction.

Single SDK-agnostic primitive (``LLMClient``) that both the policy gate
and the eval-harness judge consume. Concrete adapters live in
``app/llm/`` (one per provider). Adding a provider is a new file under
``app/llm/`` plus a wiring change at the construction edges
(``app/main.py``, ``eval/clients/pipeline.py``,
``eval/runners/harness.py``).

See DR-23 in ``docs/design/decisions.md`` for the rationale.
"""

from core.llm.client import CompletionResult, LLMClient, TokenUsage

__all__ = ["CompletionResult", "LLMClient", "TokenUsage"]
