"""RetrievedSnippet — universal retrieved corpus chunk.

The unit of retrieval surfaced to the gate. Domain-neutral by design:
the framework makes no assumption about what kind of corpus produced
this chunk (regulatory policy documents, knowledge-base articles,
internal procedures, prior-decision archives, source code, etc.). Any
gate kind that retrieves consumes a list of these.

Per DR-21, this lives at the framework root (``core.snippet``) rather
than under any gate kind's subpackage — a rule-engine gate that does
keyword matching against a different corpus still produces
``RetrievedSnippet`` instances. The type is policy-document-flavored in
its field names (``jurisdiction``, ``section_path``) but those are
domain-customary metadata for any cited reference document, not
LLM-policy-gate-specific.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


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
            type-safe construction (e.g., reasoner/account_takeover/).
        section_path: Hierarchical path within the document
            (e.g., "5.2.3 — Authenticator Assurance Level 2").
        text: Chunk text as retrieved (verbatim from the corpus).
        relevance_score: Score produced by the retriever for this chunk
            against the query, in the retriever's own scoring scheme.
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
    relevance_score: float = Field(ge=0.0)
    retrieval_path: str
