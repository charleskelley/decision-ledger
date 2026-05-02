"""Eval-specific clients.

Currently holds:

- ``ragas.py`` — ``RagasFaithfulnessScorer`` adapter (encapsulates the
  RAGAS↔LangChain coupling per DR-22; not routed through ``LLMClient``).
- ``pipeline.py`` — ``PipelineDriver`` for the consistency and robustness
  dimensions.

LLM-call adapters (formerly ``eval/clients/openai.py:OpenAIJudgeClient``)
moved to ``app/llm/`` per DR-23. The judge consumes ``OpenAILLMClient``
directly from there.
"""
