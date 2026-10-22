"""RetrievedContext — one retrieved passage, the evidence contract (DR-34).

What every retrieval provider returns for each passage, and what the policy
gate records in its ``evidence_snapshot`` and renders into the prompt. It is
the evidence contract: a provider cannot hand the gate a passage without its
corpus, tier, document, provenance level, and the SHA-256 of its text, and
the model refuses a digest that does not match the text. The bundle keeps
by value what the model saw (``text``) and by reference how it was produced
(corpus, provider, document, version, section, span, provider chunk id).

It replaces ``RetrievedSnippet`` (DR-21, moved into the gate by DR-28),
which knew one corpus per reasoner and carried a single score.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from policy_rag_gate.corpus import ProvenanceLevel


def content_hash(text: str) -> str:
    """The SHA-256 digest (lowercase hex) of ``text`` encoded as UTF-8.

    Args:
        text: A passage as retrieved.

    Returns:
        The digest every ``RetrievedContext.content_hash`` (and the
        ``EvidenceRef.content_hash`` the gate reports) carries.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RetrievedContext(BaseModel):
    """A retrieved passage with everything an auditor needs to place it.

    Attributes:
        corpus_id: The corpus it came from (as the binding names it).
        provider: The provider that retrieved it.
        resource: The provider's resource name for the corpus, if any.
        tier: The corpus's authority tier in the binding (1 is highest).
        document_id: The source document.
        document_uri: Where the source document lives, when the provider
            gives one; ``None`` otherwise (the reference provider keeps the
            document itself, by id and version).
        title: The document's human-readable title.
        version: The document version, when the provider knows it.
        jurisdiction: The document's jurisdiction tag, when it has one.
            Enforcement's jurisdiction-conflict tier reads it through the
            gate's evidence references.
        section_path: Where in the document the passage sits (``"3.2 —
            Scope"``, ``"Parent > Child"``), when known.
        span: ``(start, end)`` character offsets of ``text`` in the stored
            document text, when the passage is a contiguous slice of it;
            ``None`` otherwise.
        text: The passage exactly as retrieved.
        content_hash: SHA-256 (lowercase hex) of ``text``; must match it.
        prompted_text: The text the prompt carried, only when the gate
            transformed the passage before prompting; ``None`` (the
            reference gate never transforms one) means ``text`` was
            prompted as is.
        rank: Position in its corpus's results, from 1. Ranks are
            comparable within a corpus only.
        scores: The provider's scores for the passage, by name (the
            reference provider: ``"rerank"`` for the cross-encoder logit,
            or ``"rrf"`` for the fused score when reranking was bypassed).
            Never compared across corpora.
        provider_chunk_id: The provider's own id for the passage.
        provenance_level: How well the provider pins the passage down.
        document_metadata: Opaque document metadata from the provider (the
            reference provider: the document's front matter, which carries
            any lineage its owner put there, such as an approver or an
            effective date).
        artifact_hash: The hash of the escrowed source artifact, once the
            gate escrows one (DR-34 part 2); ``None`` until then.
    """

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", allow_inf_nan=False
    )

    corpus_id: str
    provider: str
    resource: str | None = None
    tier: int = Field(ge=1)
    document_id: str
    document_uri: str | None = None
    title: str
    version: str | None = None
    jurisdiction: str | None = None
    section_path: str | None = None
    span: tuple[int, int] | None = None
    text: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompted_text: str | None = None
    rank: int = Field(ge=1)
    scores: dict[str, float] = Field(default_factory=dict)
    provider_chunk_id: str | None = None
    provenance_level: ProvenanceLevel
    document_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    artifact_hash: str | None = None

    @model_validator(mode="after")
    def _digest_and_span_hold(self) -> RetrievedContext:
        if content_hash(self.text) != self.content_hash:
            msg = (
                f"content_hash of the passage from {self.corpus_id}/"
                f"{self.document_id} is not the SHA-256 of its text"
            )
            raise ValueError(msg)
        if self.span is not None:
            start, end = self.span
            if start < 0 or end < start:
                msg = f"span {self.span} is not (start, end) with 0 <= start <= end"
                raise ValueError(msg)
        return self
