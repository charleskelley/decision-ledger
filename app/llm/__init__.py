"""Concrete ``LLMClient`` adapters — one module per provider.

Adapters here are the only modules in the project permitted to import
LLM SDK classes (``openai``, ``anthropic``, etc.). Everything else
depends on ``core.llm.LLMClient`` and receives a constructed adapter
at the wiring edge.
"""

from app.llm.openai import OpenAILLMClient

__all__ = ["OpenAILLMClient"]
