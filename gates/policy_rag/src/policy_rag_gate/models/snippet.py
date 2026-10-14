"""RetrievedSnippet — one retrieved corpus chunk.

The unit of retrieval the policy gate's retriever returns and records in
its ``evidence_snapshot``. Domain-neutral by design: it makes no assumption
about what kind of corpus produced the chunk (regulatory policy documents,
knowledge-base articles, internal procedures, and so on).

DR-21 placed this type at the framework root so any retrieving gate could
share it. Since DR-28 the framework stores retrieval only inside a gate's
opaque ``evidence_snapshot`` and its evidence as the universal
``core.gate.EvidenceRef``, so the type moved into the policy gate with the
retriever that produces it (Phase 1b). Another retrieving gate defines its
own. The field names (``jurisdiction``, ``section_path``) are customary
metadata for any cited reference document.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RetrievedSnippet(BaseModel):
    """A retrieved corpus chunk with source metadata.

    Each snippet carries enough metadata for the enforcement and audit
    layers to verify provenance without loading the full document. The
    framework is agnostic to the retrieval architecture — fields like
    ``retrieval_path`` carry the retriever's own vocabulary verbatim; the
    framework does not interpret or constrain it.

    Attributes:
        document_id: Identifier of the source document.
        title: Human-readable document title.
        version: Semantic version of the source document.
        jurisdiction: Regulatory jurisdiction of the source document.
            Plain string — domain code uses its own jurisdiction enum for
            type-safe construction (a ``StrEnum`` in the reasoner package).
        section_path: Hierarchical path within the document
            (e.g., "3.2 — Scope").
        text: Chunk text as retrieved (verbatim from the corpus).
        relevance_score: Score produced by the retriever for this chunk
            against the query, in the retriever's own scoring scheme: cosine
            similarity on the dense leg, the RRF score after fusion, the raw
            cross-encoder logit after reranking. Unbounded — similarities
            and logits are negative for poor matches.
        retrieval_path: Free-form label indicating which retrieval path
            produced this chunk. Reference implementation uses values
            like ``"reranked"`` or ``"rrf_only"``; the framework does
            not enforce a specific vocabulary.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    document_id: str
    title: str
    version: str
    jurisdiction: str
    section_path: str
    text: str
    relevance_score: float
    retrieval_path: str
