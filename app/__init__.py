"""DecisionLedger framework runtime — imports from core/, adds infrastructure.

This package wires core/ contracts to real infrastructure: PostgreSQL +
pgvector, Elasticsearch, and the LLM policy gate. It knows no reasoner by
name: reasoners are discovered as ``ReasonerPlugin`` objects through the
``decisionledger.reasoners`` entry point group (``app/plugins.py``) and
composed by ``app/main.py:create_app``. Nothing in the wider codebase imports
from app/ except app/ itself and reasoner packages.

Dependency rule: app/ may import from core/. core/ must never import from app/.
app/ must never import from a reasoner package.

Modules and subpackages:
    plugin:       ``ReasonerPlugin`` / ``FrameworkServices`` seam.
    plugins:      Entry-point discovery, allowlist, registry assembly.
    bootstrap:    Builds the shared infrastructure clients once per process.
    decide:       ``execute_decision`` — the framework half of every decision.
    retrieval:    Hybrid RAG — pgvector + Elasticsearch BM25, RRF,
                  cross-encoder reranking; corpus loader.
    gate:         LLM policy gate with per-reasoner versioned YAML prompts.
    enforcement:  Deterministic rule-based action resolution.
    audit:        Decision Bundle construction, persistence, and replay.
    monitoring:   Structured JSON logging and observability.
"""
