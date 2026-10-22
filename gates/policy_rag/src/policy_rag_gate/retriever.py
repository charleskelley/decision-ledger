"""Hybrid policy RAG retriever: pgvector dense + Elasticsearch BM25 + RRF.

``PolicyRetriever`` orchestrates a three-stage retrieval pipeline:

1. **Dense search** — pgvector cosine similarity via HNSW index.
2. **Sparse search** — Elasticsearch BM25 with dual-analyzer fields
   (``text`` via English analyser, ``text.keyword_search`` via standard
   analyser for exact regulatory identifiers).
3. **RRF fusion** — Reciprocal Rank Fusion (k=60) merges the two ranked lists.
4. **Rerank** — Optional cross-encoder reranking with latency-budget bypass.
   If no cross-encoder is provided, or reranking exceeds the configured
   timeout, the RRF-ordered results are returned unchanged as ``rrf_only``.

Domain-agnostic by design: callers pass a pre-rendered query string plus a
``corpus_id`` filter and optional jurisdiction / risk-tier filters.
Reasoners own query construction (each ships its own query builder).

It is the engine of the reference retrieval provider
(``policy_rag_gate.providers.reference``, DR-34), which queries it once per
corpus a reasoner's binding names. The ``corpus_id`` filter is applied in
both stores, so a query never reaches another corpus; which corpora a
reasoner may read is its registration's binding, not a chunk key.
Jurisdiction metadata filtering is applied in both the Postgres and ES queries.
Risk-tier filtering is applied in Postgres; ES filters by jurisdiction only.

Every chunk row carries its document ``title`` and its deterministic
``chunk_id`` (written by the corpus loader), so the retriever needs no
access to any corpus directory.

Usage::

    retriever = PolicyRetriever(pg_conn, es_client, model)
    chunks, path = retriever.retrieve(
        query, k=5, corpus_id="<corpus-id>", jurisdictions=["REGION_A"]
    )
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from psycopg.rows import dict_row

if TYPE_CHECKING:
    import psycopg
    from elasticsearch import Elasticsearch
    from sentence_transformers import CrossEncoder, SentenceTransformer

log = structlog.get_logger(__name__)

_ES_INDEX = "policy-chunks"

# RRF constant (k=60 from the original Cormack et al. paper)
_RRF_K = 60


@dataclass(frozen=True)
class ScoredChunk:
    """One chunk a retrieval stage returned, with that stage's score.

    The retriever's own unit; the reference provider turns each into the
    gate's evidence contract (``RetrievedContext``).

    Attributes:
        chunk_id: The chunk's deterministic id (the loader's
            ``sha256(corpus_id, document_id, version, section_path)``).
        document_id: Identifier of the source document.
        title: Human-readable document title.
        version: Version of the source document.
        jurisdiction: Jurisdiction tag of the source document.
        section_path: Hierarchical path within the document.
        text: Chunk text as stored.
        relevance_score: Score from the stage that produced this ranking:
            cosine similarity on the dense leg, the RRF score after fusion,
            the raw cross-encoder logit after reranking. Unbounded.
        retrieval_path: ``"reranked"`` or ``"rrf_only"``.
        span: ``(start, end)`` of ``text`` in the stored document text, or
            ``None`` when the chunk is not a contiguous slice of it.
    """

    chunk_id: str
    document_id: str
    title: str
    version: str
    jurisdiction: str
    section_path: str
    text: str
    relevance_score: float
    retrieval_path: str
    span: tuple[int, int] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _span(start: Any, end: Any) -> tuple[int, int] | None:
    return (int(start), int(end)) if start is not None and end is not None else None


# ---------------------------------------------------------------------------
# Retriever
# ---------------------------------------------------------------------------


class PolicyRetriever:
    """Hybrid RAG retriever over the reference provider's corpus stores.

    Combines pgvector dense retrieval and Elasticsearch BM25 sparse retrieval
    via Reciprocal Rank Fusion. An optional cross-encoder reranks the fused
    results; if no cross-encoder is provided or reranking exceeds the
    configured latency budget, RRF-ordered results are returned unchanged.

    Args:
        pg_conn: Open psycopg connection with pgvector adapter registered
            (``register_vector`` must be called by the caller before passing).
        es: Connected Elasticsearch client.
        model: Loaded SentenceTransformer model for query embedding.
        cross_encoder: Optional CrossEncoder model for reranking. If ``None``,
            ``rerank()`` returns RRF-ordered results as ``"rrf_only"``.
        rerank_timeout_ms: Maximum milliseconds to spend on reranking before
            falling back to ``"rrf_only"``. Default: 200 ms.
    """

    def __init__(
        self,
        pg_conn: psycopg.Connection,
        es: Elasticsearch,
        model: SentenceTransformer,
        *,
        cross_encoder: CrossEncoder | None = None,
        rerank_timeout_ms: float = 200.0,
    ) -> None:
        """Bind the store clients and models; no corpus files are read."""
        self._pg = pg_conn
        self._es = es
        self._model = model
        self._cross_encoder = cross_encoder
        self._rerank_timeout_ms = rerank_timeout_ms

    @property
    def reranks(self) -> bool:
        """Whether a cross-encoder is configured (the ranking requested)."""
        return self._cross_encoder is not None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def dense_search(
        self,
        query: str,
        k: int,
        *,
        corpus_id: str | None = None,
        jurisdictions: list[str] | None = None,
        risk_tier: str | None = None,
    ) -> list[ScoredChunk]:
        """Pgvector cosine similarity search via HNSW index.

        Applies corpus, jurisdiction, and risk-tier metadata filters before
        the nearest-neighbour search. ``risk_tier`` filter includes chunks
        where ``risk_tier`` is NULL (applies to all tiers).

        Args:
            query: Natural language query string.
            k: Maximum number of results to return.
            corpus_id: If provided, restrict to chunks of this corpus. The
                reference provider always passes it; it is optional only to
                ease test fixtures.
            jurisdictions: If provided, restrict to these jurisdiction codes.
            risk_tier: If provided, include chunks for this tier or all tiers.

        Returns:
            Ranked chunks with cosine similarity scores.
        """
        embedding = self._model.encode([query])[0]

        filter_parts: list[str] = []
        filter_params: list[Any] = []

        if corpus_id:
            filter_parts.append("corpus_id = %s")
            filter_params.append(corpus_id)

        if jurisdictions:
            filter_parts.append("jurisdiction = ANY(%s)")
            filter_params.append(jurisdictions)

        if risk_tier:
            filter_parts.append("(risk_tier = %s OR risk_tier IS NULL)")
            filter_params.append(risk_tier)

        where = ("WHERE " + " AND ".join(filter_parts)) if filter_parts else ""

        sql = f"""
            SELECT chunk_id, policy_id, policy_version, title, jurisdiction,
                   section_path, chunk_text, span_start, span_end,
                   (1 - (embedding <=> %s)) AS relevance_score
            FROM policy_rag.policy_chunks
            {where}
            ORDER BY embedding <=> %s
            LIMIT %s
        """  # noqa: S608  # filter_parts contains only hardcoded column names
        params: list[Any] = [embedding, *filter_params, embedding, k]

        with self._pg.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)  # type: ignore[arg-type]
            rows = cur.fetchall()

        return [self._row_to_chunk(row, retrieval_path="rrf_only") for row in rows]

    def sparse_search(
        self,
        query: str,
        k: int,
        *,
        corpus_id: str | None = None,
        jurisdictions: list[str] | None = None,
    ) -> list[ScoredChunk]:
        """Elasticsearch BM25 search with dual-analyzer query.

        Queries both ``text`` (English analyzer — stemming, stopwords) and
        ``text.keyword_search`` (standard analyzer — exact identifiers such
        as section numbers or policy ids like ``POLICY-A-v2.1``). See DR-13.

        Args:
            query: Natural language query string.
            k: Maximum number of results to return.
            corpus_id: If provided, restrict to documents of this corpus.
            jurisdictions: If provided, restrict to these jurisdiction codes.

        Returns:
            Ranked chunks. Scores are raw BM25 values (not normalised) — used
            only for ranking, not as final relevance.
        """
        es_query: dict[str, Any] = {
            "bool": {
                "must": {
                    "multi_match": {
                        "query": query,
                        "fields": ["text", "text.keyword_search"],
                        "type": "best_fields",
                    }
                }
            }
        }

        es_filters: list[dict[str, Any]] = []
        if corpus_id:
            es_filters.append({"term": {"corpus_id": corpus_id}})
        if jurisdictions:
            es_filters.append({"terms": {"jurisdiction": jurisdictions}})
        if es_filters:
            es_query["bool"]["filter"] = es_filters

        response = self._es.search(index=_ES_INDEX, query=es_query, size=k)
        hits = response["hits"]["hits"]

        return [
            self._source_to_chunk(hit["_source"], retrieval_path="rrf_only")
            for hit in hits
        ]

    def rrf_fuse(
        self,
        dense: list[ScoredChunk],
        sparse: list[ScoredChunk],
        k: int,
    ) -> list[ScoredChunk]:
        """Reciprocal Rank Fusion of dense and sparse result lists.

        Deduplication key is the chunk's ``chunk_id`` (deterministic, and the
        same in both stores). When the same chunk appears in both lists, its
        RRF score accumulates from both rankings. The combined list is sorted
        by descending RRF score and truncated to ``k``.

        Args:
            dense: Ranked list from dense (pgvector) search.
            sparse: Ranked list from sparse (BM25) search.
            k: Maximum number of results to return.

        Returns:
            Fused and re-ranked list with RRF scores as ``relevance_score``.
        """
        scores: dict[str, float] = {}
        chunks: dict[str, ScoredChunk] = {}

        for rank, chunk in enumerate(dense, 1):
            key = chunk.chunk_id
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank)
            chunks[key] = chunk

        for rank, chunk in enumerate(sparse, 1):
            key = chunk.chunk_id
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank)
            if key not in chunks:
                chunks[key] = chunk

        sorted_keys = sorted(scores, key=lambda key: scores[key], reverse=True)
        return [
            dataclasses.replace(chunks[key], relevance_score=scores[key])
            for key in sorted_keys[:k]
        ]

    def rerank(
        self,
        query: str,
        candidates: list[ScoredChunk],
        k: int,
    ) -> tuple[list[ScoredChunk], str]:
        """Rerank candidates with a cross-encoder, with a latency-budget bypass.

        Scores each (query, chunk.text) pair using the cross-encoder and
        returns the top-k by descending score. If no cross-encoder was
        provided, or if scoring exceeds ``rerank_timeout_ms``, returns the
        RRF-ordered candidates unchanged as ``"rrf_only"``.

        Args:
            query: Natural language query string.
            candidates: RRF-fused candidate list to rerank.
            k: Maximum results to return.

        Returns:
            Tuple of ``(ranked_chunks, retrieval_path)`` where
            ``retrieval_path`` is ``"reranked"`` on success or ``"rrf_only"``
            on bypass.
        """
        if self._cross_encoder is None or not candidates:
            return candidates[:k], "rrf_only"

        pairs = [(query, c.text) for c in candidates]
        t0 = time.perf_counter()
        scores = self._cross_encoder.predict(pairs)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        if elapsed_ms > self._rerank_timeout_ms:
            log.warning(
                "retrieval.rerank_timeout",
                component="retrieval",
                elapsed_ms=round(elapsed_ms, 1),
                timeout_ms=self._rerank_timeout_ms,
            )
            return candidates[:k], "rrf_only"

        ranked = sorted(
            zip(scores, candidates, strict=False),
            key=lambda pair: pair[0],
            reverse=True,
        )
        result = [
            dataclasses.replace(
                chunk, relevance_score=float(score), retrieval_path="reranked"
            )
            for score, chunk in ranked[:k]
        ]
        return result, "reranked"

    def retrieve(
        self,
        query: str,
        k: int = 5,
        *,
        corpus_id: str | None = None,
        jurisdictions: list[str] | None = None,
        risk_tier: str | None = None,
    ) -> tuple[list[ScoredChunk], str]:
        """Run the full hybrid retrieval pipeline over one corpus.

        Calls dense → sparse → RRF → rerank in sequence. Returns the top-k
        chunks and the retrieval path taken (``"reranked"`` or ``"rrf_only"``).

        Args:
            query: Pre-rendered retrieval query string (built by the caller —
                reasoners own query construction).
            k: Number of chunks to return.
            corpus_id: Restricts results to the chunks of this corpus.
            jurisdictions: Jurisdiction filter forwarded to both stores.
            risk_tier: Risk-tier filter forwarded to the dense store.

        Returns:
            Tuple of (top-k chunks, retrieval_path string).
        """
        # Retrieve k*2 candidates from each source to give RRF enough to work with
        candidate_k = k * 2

        dense = self.dense_search(
            query,
            candidate_k,
            corpus_id=corpus_id,
            jurisdictions=jurisdictions,
            risk_tier=risk_tier,
        )
        sparse = self.sparse_search(
            query,
            candidate_k,
            corpus_id=corpus_id,
            jurisdictions=jurisdictions,
        )
        fused = self.rrf_fuse(dense, sparse, candidate_k)
        chunks, retrieval_path = self.rerank(query, fused, k)

        log.debug(
            "retrieval.complete",
            component="retrieval",
            corpus_id=corpus_id,
            query_preview=query[:80],
            dense_count=len(dense),
            sparse_count=len(sparse),
            fused_count=len(fused),
            returned=len(chunks),
            retrieval_path=retrieval_path,
        )
        return chunks, retrieval_path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _row_to_chunk(self, row: dict[str, Any], *, retrieval_path: str) -> ScoredChunk:
        """Convert a psycopg dict row to a ``ScoredChunk``.

        The DB column ``policy_id`` (in the gate's ``policy_rag.policy_chunks``
        table) maps to ``ScoredChunk.document_id``. A blank ``title`` falls
        back to the document id.
        """
        doc_id = str(row["policy_id"])
        return ScoredChunk(
            chunk_id=str(row["chunk_id"]),
            document_id=doc_id,
            title=row["title"] or doc_id,
            version=row["policy_version"],
            jurisdiction=row["jurisdiction"],
            section_path=row["section_path"],
            text=row["chunk_text"],
            relevance_score=float(row["relevance_score"]),
            retrieval_path=retrieval_path,
            span=_span(row.get("span_start"), row.get("span_end")),
        )

    def _source_to_chunk(
        self, source: dict[str, Any], *, retrieval_path: str
    ) -> ScoredChunk:
        """Convert an ES ``_source`` dict to a ``ScoredChunk``."""
        doc_id = str(source["policy_id"])
        return ScoredChunk(
            chunk_id=str(source["chunk_id"]),
            document_id=doc_id,
            title=source.get("title") or doc_id,
            version=source["version"],
            jurisdiction=source["jurisdiction"],
            section_path=source["section_path"],
            text=source["text"],
            relevance_score=0.0,  # BM25 score replaced by RRF score after fusion
            retrieval_path=retrieval_path,
            span=_span(source.get("span_start"), source.get("span_end")),
        )
