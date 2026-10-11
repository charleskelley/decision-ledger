"""Eval-specific clients.

Currently holds:

- ``ragas.py`` — ``RagasFaithfulnessScorer`` adapter (encapsulates the
  RAGAS↔LangChain coupling per DR-22; not routed through ``LLMClient``).

The pipeline driver for the retrieval, consistency, and robustness
dimensions is reasoner-supplied: each reasoner's ``EvalAdapter`` builds
one (see ``eval.adapter``).

LLM-call adapters (formerly ``eval/clients/openai.py:OpenAIJudgeClient``)
moved to ``app/llm/`` per DR-23. The judge consumes ``OpenAILLMClient``
directly from there.
"""
