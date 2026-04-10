"""Runtime pipeline for the ATO Reasoner — imports from core/, adds infrastructure.

This package wires core/ contracts to real infrastructure: Redis Streams, PostgreSQL,
Elasticsearch, XGBoost, and the OpenAI API. Nothing in the wider codebase imports
from app/ except app/ itself.

Dependency rule: app/ may import from core/. core/ must never import from app/.

Subpackages:
    ingestion:    Redis Streams consumer, deduplication, bounded lateness.
    features:     Sliding window feature computation.
    scorer:       XGBoost fast scorer (risk score + SHAP signals).
    retrieval:    Hybrid RAG — pgvector + Elasticsearch BM25, RRF,
                  cross-encoder reranking.
    policy_gate:  LLM reasoning layer with versioned YAML prompts.
    enforcement:  Deterministic rule-based action resolution.
    audit:        Decision Bundle construction and deterministic replay.
    monitoring:   Structured JSON logging and observability.
"""
