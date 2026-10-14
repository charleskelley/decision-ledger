"""Concrete ``LLMClient`` adapters — one module per provider.

Adapters here are the only modules in the project permitted to import
LLM SDK classes (``openai``, ``anthropic``, etc.; DR-23, enforced by the CI
boundary job). Everything else — the policy gate, its eval judges —
depends on the framework's ``core.llm.LLMClient`` protocol and receives a
constructed adapter at the wiring edge. They live in the policy gate, their
only consumer (DR-28); they move into a shared package only when a second
consumer appears.
"""

from policy_rag_gate.llm.openai import OpenAILLMClient

__all__ = ["OpenAILLMClient"]
