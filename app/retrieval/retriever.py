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

Domain-agnostic by design: callers pass a pre-rendered query string plus
optional jurisdiction / risk-tier filters. Reasoners own query construction
(see ``reasoner/<domain>/retrieval_query.py`` for the ATO reference).

Jurisdiction metadata filtering is applied in both the Postgres and ES queries.
Risk-tier filtering is applied in Postgres; ES filters by jurisdiction only.

The title lookup is resolved from an in-memory map built from corpus frontmatter
at startup because the ``policy_chunks`` table does not carry a ``title`` column.

Usage::

    retriever = PolicyRetriever(pg_conn, es_client, model)
    snippets, path = retriever.retrieve(
        query, k=5, jurisdictions=["US_FEDERAL", "INTERNAL"]
    )
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from psycopg.rows import dict_row

from app.retrieval.corpus_loader import parse_document
from core.snippet import RetrievedSnippet

if TYPE_CHECKING:
    import psycopg
    from elasticsearch import Elasticsearch
    from sentence_transformers import CrossEncoder, SentenceTransformer

log = structlog.get_logger(__name__)

_ES_INDEX = "policy-chunks"
_PROJECT_ROOT = Path(__file__).parents[2]
_CORPUS_DIR = _PROJECT_ROOT / "corpus"

# RRF constant (k=60 from the original Cormack et al. paper)
_RRF_K = 60

# Dedup key type for RRF: (policy_id, version, section_path)
_SnippetKey = tuple[str, str, str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snippet_key(s: RetrievedSnippet) -> _SnippetKey:
    return (s.document_id, s.version, s.section_path)


def _load_title_map(corpus_dir: Path) -> dict[str, str]:
    """Build a policy_id → title lookup from corpus frontmatter.

    Args:
        corpus_dir: Root directory of the policy corpus.

    Returns:
        Mapping from ``policy_id`` to document title.
    """
    title_map: dict[str, str] = {}
    for path in corpus_dir.rglob("*.md"):
        try:
            doc, _ = parse_document(path)
            title_map[doc.policy_id] = doc.title
        except (ValueError, KeyError):
            pass
    return title_map


# ---------------------------------------------------------------------------
# Retriever
# ---------------------------------------------------------------------------


class PolicyRetriever:
    """Hybrid RAG retriever for the ATO Reasoner policy corpus.

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
        corpus_dir: Corpus root used to build the title lookup map.
    """

    def __init__(
        self,
        pg_conn: psycopg.Connection,
        es: Elasticsearch,
        model: SentenceTransformer,
        *,
        cross_encoder: CrossEncoder | None = None,
        rerank_timeout_ms: float = 200.0,
        corpus_dir: Path = _CORPUS_DIR,
    ) -> None:
        """Initialize the retriever and build the title map from corpus."""
        self._pg = pg_conn
        self._es = es
        self._model = model
        self._cross_encoder = cross_encoder
        self._rerank_timeout_ms = rerank_timeout_ms
        self._titles = _load_title_map(corpus_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def dense_search(
        self,
        query: str,
        k: int,
        *,
        reasoner_id: str | None = None,
        jurisdictions: list[str] | None = None,
        risk_tier: str | None = None,
    ) -> list[RetrievedSnippet]:
        """Pgvector cosine similarity search via HNSW index.

        Applies reasoner, jurisdiction, and risk-tier metadata filters before
        the nearest-neighbour search. ``risk_tier`` filter includes chunks
        where ``risk_tier`` is NULL (applies to all tiers).

        Args:
            query: Natural language query string.
            k: Maximum number of results to return.
            reasoner_id: If provided, restrict to chunks belonging to this
                reasoner's corpus. Required at the framework boundary; the
                MVP keeps it optional only to ease test fixtures.
            jurisdictions: If provided, restrict to these jurisdiction codes.
            risk_tier: If provided, include chunks for this tier or all tiers.

        Returns:
            Ranked list of RetrievedSnippet objects with cosine similarity scores.
        """
        embedding = self._model.encode([query])[0]

        filter_parts: list[str] = []
        filter_params: list[Any] = []

        if reasoner_id:
            filter_parts.append("reasoner_id = %s")
            filter_params.append(reasoner_id)

        if jurisdictions:
            filter_parts.append("jurisdiction = ANY(%s)")
            filter_params.append(jurisdictions)

        if risk_tier:
            filter_parts.append("(risk_tier = %s OR risk_tier IS NULL)")
            filter_params.append(risk_tier)

        where = ("WHERE " + " AND ".join(filter_parts)) if filter_parts else ""

        sql = f"""
            SELECT policy_id, policy_version, jurisdiction, section_path,
                   chunk_text, (1 - (embedding <=> %s)) AS relevance_score
            FROM decisionledger.policy_chunks
            {where}
            ORDER BY embedding <=> %s
            LIMIT %s
        """  # noqa: S608  # filter_parts contains only hardcoded column names
        params: list[Any] = [embedding, *filter_params, embedding, k]

        with self._pg.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)  # type: ignore[arg-type]
            rows = cur.fetchall()

        return [self._row_to_snippet(row, retrieval_path="rrf_only") for row in rows]

    def sparse_search(
        self,
        query: str,
        k: int,
        *,
        reasoner_id: str | None = None,
        jurisdictions: list[str] | None = None,
    ) -> list[RetrievedSnippet]:
        """Elasticsearch BM25 search with dual-analyzer query.

        Queries both ``text`` (English analyser — stemming, stopwords) and
        ``text.keyword_search`` (standard analyser — exact regulatory
        identifiers like ``AAL2``, ``NIST-SP-800-63B``). See DR-13.

        Args:
            query: Natural language query string.
            k: Maximum number of results to return.
            reasoner_id: If provided, restrict to documents belonging to this
                reasoner's corpus.
            jurisdictions: If provided, restrict to these jurisdiction codes.

        Returns:
            Ranked list of RetrievedSnippet objects. Scores are raw BM25 values
            (not normalised) — used only for ranking, not as final relevance.
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
        if reasoner_id:
            es_filters.append({"term": {"reasoner_id": reasoner_id}})
        if jurisdictions:
            es_filters.append({"terms": {"jurisdiction": jurisdictions}})
        if es_filters:
            es_query["bool"]["filter"] = es_filters

        response = self._es.search(index=_ES_INDEX, query=es_query, size=k)
        hits = response["hits"]["hits"]

        snippets = []
        for hit in hits:
            source = hit["_source"]
            snippets.append(self._source_to_snippet(source, retrieval_path="rrf_only"))
        return snippets

    def rrf_fuse(
        self,
        dense: list[RetrievedSnippet],
        sparse: list[RetrievedSnippet],
        k: int,
    ) -> list[RetrievedSnippet]:
        """Reciprocal Rank Fusion of dense and sparse result lists.

        Deduplication key is ``(policy_id, version, section_path)``. When the
        same chunk appears in both lists, its RRF score accumulates from both
        rankings. The combined list is sorted by descending RRF score and
        truncated to ``k``.

        Args:
            dense: Ranked list from dense (pgvector) search.
            sparse: Ranked list from sparse (BM25) search.
            k: Maximum number of results to return.

        Returns:
            Fused and re-ranked list with RRF scores as ``relevance_score``.
        """
        scores: dict[_SnippetKey, float] = {}
        chunks: dict[_SnippetKey, RetrievedSnippet] = {}

        for rank, snippet in enumerate(dense, 1):
            key = _snippet_key(snippet)
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank)
            chunks[key] = snippet

        for rank, snippet in enumerate(sparse, 1):
            key = _snippet_key(snippet)
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank)
            if key not in chunks:
                chunks[key] = snippet

        sorted_keys = sorted(scores, key=lambda key: scores[key], reverse=True)
        return [
            chunks[key].model_copy(update={"relevance_score": scores[key]})
            for key in sorted_keys[:k]
        ]

    def rerank(
        self,
        query: str,
        candidates: list[RetrievedSnippet],
        k: int,
    ) -> tuple[list[RetrievedSnippet], str]:
        """Rerank candidates with a cross-encoder, with a latency-budget bypass.

        Scores each (query, snippet.text) pair using the cross-encoder and
        returns the top-k by descending score. If no cross-encoder was
        provided, or if scoring exceeds ``rerank_timeout_ms``, returns the
        RRF-ordered candidates unchanged as ``"rrf_only"``.

        Args:
            query: Natural language query string.
            candidates: RRF-fused candidate list to rerank.
            k: Maximum results to return.

        Returns:
            Tuple of ``(ranked_snippets, retrieval_path)`` where
            ``retrieval_path`` is ``"reranked"`` on success or ``"rrf_only"``
            on bypass.
        """
        if self._cross_encoder is None or not candidates:
            return candidates[:k], "rrf_only"

        pairs = [(query, s.text) for s in candidates]
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
            snippet.model_copy(
                update={
                    "relevance_score": float(score),
                    "retrieval_path": "reranked",
                }
            )
            for score, snippet in ranked[:k]
        ]
        return result, "reranked"

    def retrieve(
        self,
        query: str,
        k: int = 5,
        *,
        reasoner_id: str | None = None,
        jurisdictions: list[str] | None = None,
        risk_tier: str | None = None,
    ) -> tuple[list[RetrievedSnippet], str]:
        """Run the full hybrid retrieval pipeline.

        Calls dense → sparse → RRF → rerank in sequence. Returns the top-k
        snippets and the retrieval path taken (``"reranked"`` or ``"rrf_only"``).

        Args:
            query: Pre-rendered retrieval query string (built by the caller —
                reasoners own query construction).
            k: Number of snippets to return.
            reasoner_id: Tenant filter — restricts results to chunks belonging
                to the calling reasoner's corpus.
            jurisdictions: Jurisdiction filter forwarded to both stores.
            risk_tier: Risk-tier filter forwarded to the dense store.

        Returns:
            Tuple of (top-k RetrievedSnippet list, retrieval_path string).
        """
        # Retrieve k*2 candidates from each source to give RRF enough to work with
        candidate_k = k * 2

        dense = self.dense_search(
            query,
            candidate_k,
            reasoner_id=reasoner_id,
            jurisdictions=jurisdictions,
            risk_tier=risk_tier,
        )
        sparse = self.sparse_search(
            query,
            candidate_k,
            reasoner_id=reasoner_id,
            jurisdictions=jurisdictions,
        )
        fused = self.rrf_fuse(dense, sparse, candidate_k)
        snippets, retrieval_path = self.rerank(query, fused, k)

        log.debug(
            "retrieval.complete",
            component="retrieval",
            query_preview=query[:80],
            dense_count=len(dense),
            sparse_count=len(sparse),
            fused_count=len(fused),
            returned=len(snippets),
            retrieval_path=retrieval_path,
        )
        return snippets, retrieval_path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _row_to_snippet(
        self, row: dict[str, Any], *, retrieval_path: str
    ) -> RetrievedSnippet:
        """Convert a psycopg dict row to a RetrievedSnippet.

        The DB column ``policy_id`` (in the ``policy_chunks`` table) maps
        to ``RetrievedSnippet.document_id`` — the column lives in a
        policy-corpus-specific schema; the snippet type is domain-neutral.
        """
        doc_id = str(row["policy_id"])
        return RetrievedSnippet(
            document_id=doc_id,
            title=self._titles.get(doc_id, doc_id),
            version=row["policy_version"],
            jurisdiction=row["jurisdiction"],
            section_path=row["section_path"],
            text=row["chunk_text"],
            relevance_score=float(row["relevance_score"]),
            retrieval_path=retrieval_path,
        )

    def _source_to_snippet(
        self, source: dict[str, Any], *, retrieval_path: str
    ) -> RetrievedSnippet:
        """Convert an ES ``_source`` dict to a RetrievedSnippet."""
        doc_id = str(source["policy_id"])
        return RetrievedSnippet(
            document_id=doc_id,
            title=source.get("title", self._titles.get(doc_id, doc_id)),
            version=source["version"],
            jurisdiction=source["jurisdiction"],
            section_path=source["section_path"],
            text=source["text"],
            relevance_score=0.0,  # BM25 score replaced by RRF score after fusion
            retrieval_path=retrieval_path,
        )
