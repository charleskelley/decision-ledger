"""The reference retrieval provider — pgvector + Elasticsearch + cross-encoder.

Today's hybrid retrieval stack behind the provider seam (DR-34): the
``PolicyRetriever`` (dense pgvector search, BM25 in Elasticsearch,
Reciprocal Rank Fusion, cross-encoder reranking with its latency-budget
bypass, unchanged) over the corpora the corpus loader loads into the
``policy_rag`` schema and the ``policy-chunks`` index, each under its
``corpus_id``.

It holds every corpus whole (``policy_rag.documents``, by document and
version), so its passages carry ``pinned_version`` provenance; its state
marker for a corpus is the manifest hash the loader last recorded in
``policy_rag.corpora``. Postgres is authoritative: a hit is returned only
if Postgres holds that chunk with that text, so an Elasticsearch document a
failed or interrupted load left behind never reaches a prompt. A corpus it
does not hold cannot be described, so the gate refuses a binding that names
one: load the corpus first.

It is a ``DocumentFetcher`` too (DR-34 part 2): ``fetch_document`` returns
one document version whole from ``policy_rag.documents``, over the
provider's own Postgres connection (Elasticsearch is never involved), for
the gate to escrow when it decides and to compare with when it verifies
live.

Published as ``reference`` under the ``policy_rag_gate.providers`` entry-point
group (``PLUGIN``); the gate builds it the first time a binding names it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

import psycopg
import structlog
from elasticsearch import Elasticsearch
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from sentence_transformers import CrossEncoder, SentenceTransformer

from policy_rag_gate.corpus import CorpusRef, CorpusState
from policy_rag_gate.models.context import RetrievedContext, content_hash
from policy_rag_gate.providers.protocol import (
    DocumentBlob,
    DocumentNotHeldError,
    ProviderResult,
    ProviderServices,
)
from policy_rag_gate.retriever import PolicyRetriever
from policy_rag_gate.schema import apply_schema

if TYPE_CHECKING:
    from collections.abc import Sequence

    from policy_rag_gate.retriever import ScoredChunk

#: The reference provider's id (what ``CorpusRef.provider`` defaults to).
REFERENCE_PROVIDER_ID = "reference"
#: Sentence-transformer used to embed queries (must match the corpus loader).
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
#: Cross-encoder used to rerank fused candidates.
CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
#: The provider's version label, recorded in every ``CorpusState``.
REFERENCE_PROVIDER_VERSION = f"reference-1 ({EMBEDDING_MODEL}; {CROSS_ENCODER_MODEL})"

_CORPUS_STATE = """
    select manifest_hash, document_count, loaded_at
    from policy_rag.corpora
    where corpus_id = %s
"""

# The chunks Postgres holds, by id, with their documents' front matter.
_HELD_CHUNKS = """
    select c.chunk_id, c.chunk_text, d.metadata
    from policy_rag.policy_chunks as c
    inner join policy_rag.documents as d
        on
            c.corpus_id = d.corpus_id
            and c.policy_id = d.document_id
            and c.policy_version = d.document_version
    where c.corpus_id = %s and c.chunk_id = any(%s)
"""

# One document version, whole; no row means the corpus is not loaded, a row
# with no body that it holds no such version.
_DOCUMENT = """
    select d.title, d.body, d.metadata
    from policy_rag.corpora as c
    left join policy_rag.documents as d
        on
            c.corpus_id = d.corpus_id
            and d.document_id = %s
            and d.document_version = %s
    where c.corpus_id = %s
"""

log = structlog.get_logger(__name__)


class _Closeable(Protocol):
    def close(self) -> None: ...


class CorpusNotLoadedError(LookupError):
    """The reference provider holds no corpus by that id."""


class ReferenceProvider:
    """``RetrievalProvider`` over the gate's own stores.

    Args:
        retriever: The hybrid retriever (its stores and models).
        pg_conn: The autocommit connection the retriever reads, for corpus
            state and document metadata.
        resources: Handles to close on ``close()``.
    """

    provider_id: str = REFERENCE_PROVIDER_ID

    def __init__(
        self,
        *,
        retriever: PolicyRetriever,
        pg_conn: psycopg.Connection[Any],
        resources: Sequence[_Closeable] = (),
    ) -> None:
        """Bind the retriever and the connection it reads."""
        self._retriever = retriever
        self._pg = pg_conn
        self._resources = tuple(resources)

    @property
    def retriever(self) -> PolicyRetriever:
        """The hybrid retriever behind this provider."""
        return self._retriever

    def close(self) -> None:
        """Release the stores' handles."""
        for resource in self._resources:
            resource.close()

    def describe(self, corpus: CorpusRef) -> CorpusState:
        """Report a loaded corpus's manifest hash, size, and load time.

        Args:
            corpus: The reference (the reference provider takes no
                ``resource``).

        Returns:
            Its state, with ``pinned_version`` provenance.

        Raises:
            ValueError: The reference names a ``resource``.
            CorpusNotLoadedError: No corpus by that id is loaded.
        """
        _takes_no_resource(corpus)
        with self._pg.cursor(row_factory=dict_row) as cur:
            cur.execute(_CORPUS_STATE, (corpus.corpus_id,))
            row = cur.fetchone()
        if row is None:
            raise CorpusNotLoadedError(_not_loaded(corpus))
        return CorpusState(
            corpus_id=corpus.corpus_id,
            provider=REFERENCE_PROVIDER_ID,
            provider_version=REFERENCE_PROVIDER_VERSION,
            state_marker=str(row["manifest_hash"]),
            provenance_level="pinned_version",
            document_count=int(row["document_count"]),
            updated_at=row["loaded_at"],
        )

    def retrieve(
        self,
        query: str,
        corpus: CorpusRef,
        *,
        k: int,
        jurisdictions: Sequence[str] | None,
        risk_tier: str | None,
    ) -> ProviderResult:
        """Hybrid retrieval over one loaded corpus.

        Args:
            query: The reasoner-rendered retrieval query.
            corpus: The corpus (its tier is stamped on every passage).
            k: Most passages to return.
            jurisdictions: Filter applied in both stores, or ``None``.
            risk_tier: Filter applied in the dense store only, or ``None``.

        Returns:
            The passages, ranked within the corpus, and its state.

        Raises:
            ValueError, CorpusNotLoadedError: As ``describe``.
            Exception: Whatever the stores raise (an outage propagates, and
                the gate fails closed).
        """
        state = self.describe(corpus)
        requested = "reranked" if self._retriever.reranks else "rrf_only"
        chunks, ranking_applied = self._retriever.retrieve(
            query,
            k,
            corpus_id=corpus.corpus_id,
            jurisdictions=list(jurisdictions) if jurisdictions else None,
            risk_tier=risk_tier,
        )
        if not chunks:
            # Nothing matched, so nothing was ranked and nothing bypassed.
            ranking_applied = requested
        held = self._held(corpus.corpus_id, chunks)
        kept = [
            chunk
            for chunk in chunks
            if chunk.chunk_id in held and held[chunk.chunk_id][0] == chunk.text
        ]
        if len(kept) < len(chunks):
            log.warning(
                "reference_provider.unheld_hits_dropped",
                component="retrieval",
                corpus_id=corpus.corpus_id,
                dropped=len(chunks) - len(kept),
            )
        contexts = tuple(
            _context(chunk, corpus=corpus, rank=rank, metadata=held[chunk.chunk_id][1])
            for rank, chunk in enumerate(kept, 1)
        )
        return ProviderResult(
            state=state,
            contexts=contexts,
            ranking_requested=requested,
            ranking_applied=ranking_applied,
        )

    def _held(
        self, corpus_id: str, chunks: Sequence[ScoredChunk]
    ) -> dict[str, tuple[str, dict[str, Any]]]:
        """What Postgres holds of ``chunks``: text and document front matter, by id."""
        ids = sorted({chunk.chunk_id for chunk in chunks})
        if not ids:
            return {}
        with self._pg.cursor(row_factory=dict_row) as cur:
            cur.execute(_HELD_CHUNKS, (corpus_id, ids))
            rows = cur.fetchall()
        return {
            str(row["chunk_id"]): (str(row["chunk_text"]), dict(row["metadata"] or {}))
            for row in rows
        }

    def fetch_document(
        self, corpus: CorpusRef, document_id: str, version: str | None
    ) -> DocumentBlob:
        """Return one loaded document version whole (``DocumentFetcher``).

        Reads ``policy_rag.documents`` over the provider's own Postgres
        connection; Elasticsearch is never involved. ``text`` is the body the
        loader stored (front matter stripped), which every chunk's span
        indexes.

        Args:
            corpus: The reference (the reference provider takes no
                ``resource``).
            document_id: The document.
            version: The version: required, since a corpus may hold several
                versions of one document.

        Returns:
            The document, its front matter as ``metadata``.

        Raises:
            ValueError: The reference names a ``resource``, or no version is
                given.
            CorpusNotLoadedError: No corpus by that id is loaded.
            DocumentNotHeldError: The corpus is loaded, but not that
                document version (removed or re-versioned by a reload).
        """
        _takes_no_resource(corpus)
        if version is None:
            msg = (
                f"the reference provider fetches {corpus.corpus_id}/{document_id} "
                "by version: a corpus may hold several versions of one document"
            )
            raise ValueError(msg)
        with self._pg.cursor(row_factory=dict_row) as cur:
            cur.execute(_DOCUMENT, (document_id, version, corpus.corpus_id))
            row = cur.fetchone()
        if row is None:
            raise CorpusNotLoadedError(_not_loaded(corpus))
        if row["body"] is None:
            msg = (
                f"corpus {corpus.corpus_id!r} holds no version {version!r} of "
                f"document {document_id!r}"
            )
            raise DocumentNotHeldError(msg)
        text = str(row["body"])
        return DocumentBlob(
            corpus_id=corpus.corpus_id,
            document_id=document_id,
            version=version,
            title=str(row["title"]),
            text=text,
            content_hash=content_hash(text),
            metadata=dict(row["metadata"] or {}),
        )


def _takes_no_resource(corpus: CorpusRef) -> None:
    if corpus.resource is not None:
        msg = (
            f"the reference provider takes no resource (corpus "
            f"{corpus.corpus_id!r} names {corpus.resource!r}): it holds each "
            "corpus under its corpus_id"
        )
        raise ValueError(msg)


def _not_loaded(corpus: CorpusRef) -> str:
    return (
        f"corpus {corpus.corpus_id!r} is not loaded in the reference "
        "provider: load it with python -m policy_rag_gate.corpus_loader "
        f"--corpus-id {corpus.corpus_id} (and --reasoner-id or --corpus-dir "
        "to say where its documents are)"
    )


def _context(
    chunk: ScoredChunk, *, corpus: CorpusRef, rank: int, metadata: dict[str, Any]
) -> RetrievedContext:
    """One retrieved chunk under the evidence contract."""
    score_name = "rerank" if chunk.retrieval_path == "reranked" else "rrf"
    return RetrievedContext(
        corpus_id=corpus.corpus_id,
        provider=REFERENCE_PROVIDER_ID,
        resource=None,
        tier=corpus.tier,
        document_id=chunk.document_id,
        document_uri=None,
        title=chunk.title,
        version=chunk.version,
        jurisdiction=chunk.jurisdiction or None,
        section_path=chunk.section_path,
        span=chunk.span,
        text=chunk.text,
        content_hash=content_hash(chunk.text),
        rank=rank,
        scores={score_name: chunk.relevance_score},
        provider_chunk_id=chunk.chunk_id,
        provenance_level="pinned_version",
        document_metadata=metadata,
    )


def open_reference_provider(
    *, postgres_dsn: str, elasticsearch_url: str
) -> ReferenceProvider:
    """Open the reference provider's stores and models.

    Applies the gate's own DDL first, so a fresh database gets the vector
    extension and the ``policy_rag`` schema before the pgvector adapters are
    registered (and a schema from before DR-34 is refused).

    Args:
        postgres_dsn: DSN of the database that holds the ``policy_rag``
            schema (the framework's database; the gate owns the schema).
        elasticsearch_url: URL of the gate's Elasticsearch.

    Returns:
        The provider. ``close()`` releases the Elasticsearch client, then the
        autocommit Postgres connection. On failure the connection is closed
        before the error propagates.
    """
    pg_conn = psycopg.connect(postgres_dsn, autocommit=True)
    try:
        apply_schema(pg_conn)
        register_vector(pg_conn)
        es = Elasticsearch(elasticsearch_url)
        retriever = PolicyRetriever(
            pg_conn=pg_conn,
            es=es,
            model=SentenceTransformer(EMBEDDING_MODEL),
            cross_encoder=CrossEncoder(CROSS_ENCODER_MODEL),
        )
    except Exception:
        pg_conn.close()
        raise
    return ReferenceProvider(
        retriever=retriever, pg_conn=pg_conn, resources=(es, pg_conn)
    )


class ReferenceProviderPlugin:
    """``ProviderPlugin`` for the reference provider.

    Attributes:
        provider_id: ``"reference"``.
    """

    provider_id: str = REFERENCE_PROVIDER_ID

    def build(self, services: ProviderServices) -> ReferenceProvider:
        """Open the provider on the framework's database and the gate's ES.

        Args:
            services: The framework's DSN and the gate's settings
                (``POLICY_RAG_GATE_ELASTICSEARCH_URL``).

        Returns:
            The provider.
        """
        return open_reference_provider(
            postgres_dsn=services.postgres_dsn,
            elasticsearch_url=services.settings.elasticsearch_url,
        )


#: The entry-point target: ``reference = "policy_rag_gate.providers.reference:PLUGIN"``.
PLUGIN = ReferenceProviderPlugin()
