"""Unit tests for PolicyRetriever pure functions and row mapping.

Tests for rrf_fuse, rerank, and the row/source → snippet conversions do not
require Docker — infrastructure dependencies are mocked at construction
time. The retriever reads no corpus files: every chunk row carries its
document ``title``. Domain-specific query construction lives with each
reasoner (``reasoner/<domain>/retrieval_query.py``) and is tested there.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from policy_rag_gate.models.snippet import RetrievedSnippet
from policy_rag_gate.retriever import PolicyRetriever

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_retriever() -> PolicyRetriever:
    """PolicyRetriever with mocked infra — only pure methods are testable."""
    return PolicyRetriever(
        pg_conn=MagicMock(),
        es=MagicMock(),
        model=MagicMock(),
    )


def _make_snippet(
    document_id: str,
    section_path: str = "Overview",
    *,
    relevance_score: float = 0.5,
) -> RetrievedSnippet:
    return RetrievedSnippet(
        document_id=document_id,
        title=document_id,
        version="1.0",
        jurisdiction="US_FEDERAL",
        section_path=section_path,
        text="Sample text.",
        relevance_score=relevance_score,
        retrieval_path="rrf_only",
    )


# ---------------------------------------------------------------------------
# rrf_fuse
# ---------------------------------------------------------------------------


def test_rrf_fuse_accumulates_scores_for_overlapping_chunk():
    """A chunk appearing in both dense and sparse should outscore exclusives."""
    r = _make_retriever()
    shared = _make_snippet("SHARED-DOC")
    dense_only = _make_snippet("DENSE-ONLY")
    sparse_only = _make_snippet("SPARSE-ONLY")

    fused = r.rrf_fuse([shared, dense_only], [shared, sparse_only], k=3)

    scores = {s.document_id: s.relevance_score for s in fused}
    assert scores["SHARED-DOC"] > scores["DENSE-ONLY"]
    assert scores["SHARED-DOC"] > scores["SPARSE-ONLY"]


def test_rrf_fuse_deduplicates_same_snippet():
    """The same chunk from both lists appears exactly once in the output."""
    r = _make_retriever()
    snippet = _make_snippet("DOC-A")

    fused = r.rrf_fuse([snippet], [snippet], k=5)

    assert len(fused) == 1
    assert fused[0].document_id == "DOC-A"


def test_rrf_fuse_truncates_to_k():
    """Output is capped at k results regardless of candidate pool size."""
    r = _make_retriever()
    dense = [_make_snippet(f"D-{i}") for i in range(5)]
    sparse = [_make_snippet(f"S-{i}") for i in range(5)]

    fused = r.rrf_fuse(dense, sparse, k=3)

    assert len(fused) == 3


def test_rrf_fuse_updates_relevance_score_to_rrf_value():
    """rrf_fuse overwrites the original relevance_score with the RRF score."""
    r = _make_retriever()
    snippet = _make_snippet("DOC-A", relevance_score=0.99)

    fused = r.rrf_fuse([snippet], [], k=1)

    # RRF score for rank-1 with k=60 is 1/61 ≈ 0.016
    assert fused[0].relevance_score < 0.1


def test_rrf_fuse_deduplicates_by_full_snippet_key():
    """Same policy_id but different section_path — these are distinct chunks."""
    r = _make_retriever()
    a = _make_snippet("DOC-A", section_path="Section 1")
    b = _make_snippet("DOC-A", section_path="Section 2")

    fused = r.rrf_fuse([a], [b], k=5)

    assert len(fused) == 2


# ---------------------------------------------------------------------------
# rerank
# ---------------------------------------------------------------------------


def test_rerank_returns_rrf_only_when_no_cross_encoder():
    """Without a cross-encoder, rerank returns top-k RRF results unchanged."""
    r = _make_retriever()
    candidates = [_make_snippet(f"DOC-{i}") for i in range(5)]

    result, path = r.rerank("some query", candidates, k=3)

    assert path == "rrf_only"
    assert result == candidates[:3]


def test_rerank_returns_rrf_only_for_empty_candidates():
    """Empty candidate list short-circuits to rrf_only regardless of encoder."""
    r = _make_retriever()

    result, path = r.rerank("some query", [], k=5)

    assert path == "rrf_only"
    assert result == []


def test_rerank_reorders_by_cross_encoder_score():
    """Cross-encoder scores override RRF order; highest score ranks first."""
    mock_ce = MagicMock()
    mock_ce.predict.return_value = [0.1, 0.9, 0.5]  # second snippet wins
    r = PolicyRetriever(
        pg_conn=MagicMock(),
        es=MagicMock(),
        model=MagicMock(),
        cross_encoder=mock_ce,
    )
    candidates = [
        _make_snippet("DOC-A"),
        _make_snippet("DOC-B"),
        _make_snippet("DOC-C"),
    ]

    result, path = r.rerank("query", candidates, k=3)

    assert path == "reranked"
    assert result[0].document_id == "DOC-B"
    assert result[1].document_id == "DOC-C"
    assert result[2].document_id == "DOC-A"


def test_rerank_updates_relevance_score_and_retrieval_path():
    """Reranked snippets carry the cross-encoder score and path='reranked'."""
    mock_ce = MagicMock()
    mock_ce.predict.return_value = [0.85]
    r = PolicyRetriever(
        pg_conn=MagicMock(),
        es=MagicMock(),
        model=MagicMock(),
        cross_encoder=mock_ce,
    )
    candidates = [_make_snippet("DOC-A", relevance_score=0.3)]

    result, path = r.rerank("query", candidates, k=1)

    assert path == "reranked"
    assert result[0].relevance_score == 0.85
    assert result[0].retrieval_path == "reranked"


def test_rerank_truncates_to_k():
    """rerank returns at most k results."""
    mock_ce = MagicMock()
    mock_ce.predict.return_value = [0.9, 0.8, 0.7, 0.6, 0.5]
    r = PolicyRetriever(
        pg_conn=MagicMock(),
        es=MagicMock(),
        model=MagicMock(),
        cross_encoder=mock_ce,
    )
    candidates = [_make_snippet(f"DOC-{i}") for i in range(5)]

    result, _ = r.rerank("query", candidates, k=2)

    assert len(result) == 2


def test_rerank_falls_back_to_rrf_on_timeout():
    """If predict takes longer than the budget, returns rrf_only."""
    call_count = 0

    def _fake_perf_counter() -> float:
        nonlocal call_count
        call_count += 1
        # First call: t0 = 0.0; second call: simulate 300 ms elapsed
        return 0.0 if call_count == 1 else 0.3

    mock_ce = MagicMock()
    mock_ce.predict.return_value = [0.9, 0.8]
    r = PolicyRetriever(
        pg_conn=MagicMock(),
        es=MagicMock(),
        model=MagicMock(),
        cross_encoder=mock_ce,
        rerank_timeout_ms=200.0,
    )
    candidates = [_make_snippet("DOC-A"), _make_snippet("DOC-B")]

    import unittest.mock as _mock

    with _mock.patch("policy_rag_gate.retriever.time.perf_counter", _fake_perf_counter):
        result, path = r.rerank("query", candidates, k=2)

    assert path == "rrf_only"
    assert result == candidates[:2]


# ---------------------------------------------------------------------------
# Row / source → snippet mapping (title comes from the stores, not the corpus)
# ---------------------------------------------------------------------------


def test_row_to_snippet_reads_title_from_the_row():
    """The dense leg cites the title stored on the chunk row."""
    r = _make_retriever()
    row = {
        "policy_id": "DOC-A",
        "policy_version": "2.0",
        "title": "Document A Title",
        "jurisdiction": "REGION_A",
        "section_path": "Overview",
        "chunk_text": "Body.",
        "relevance_score": 0.42,
    }

    snippet = r._row_to_snippet(row, retrieval_path="rrf_only")

    assert snippet.title == "Document A Title"
    assert snippet.document_id == "DOC-A"
    assert snippet.relevance_score == 0.42


def test_row_to_snippet_falls_back_to_document_id_for_pre_migration_rows():
    """Rows written before the title column existed carry '' → use the id."""
    r = _make_retriever()
    row = {
        "policy_id": "DOC-OLD",
        "policy_version": "1.0",
        "title": "",
        "jurisdiction": "REGION_A",
        "section_path": "Overview",
        "chunk_text": "Body.",
        "relevance_score": 0.1,
    }

    snippet = r._row_to_snippet(row, retrieval_path="rrf_only")

    assert snippet.title == "DOC-OLD"


def test_dense_search_selects_title_column_and_filters_by_reasoner():
    """The dense SQL reads title from Postgres and scopes by reasoner_id."""
    pg = MagicMock()
    cursor = pg.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [
        {
            "policy_id": "DOC-A",
            "policy_version": "2.0",
            "title": "Document A Title",
            "jurisdiction": "REGION_A",
            "section_path": "Overview",
            "chunk_text": "Body.",
            "relevance_score": 0.9,
        }
    ]
    model = MagicMock()
    model.encode.return_value = [[0.0] * 3]
    r = PolicyRetriever(pg_conn=pg, es=MagicMock(), model=model)

    snippets = r.dense_search("query", k=1, reasoner_id="some-reasoner")

    sql, params = cursor.execute.call_args.args
    assert "title" in sql.split("FROM")[0]
    assert "reasoner_id = %s" in sql
    assert "some-reasoner" in params
    assert snippets[0].title == "Document A Title"


def test_source_to_snippet_reads_title_from_the_es_document():
    """The sparse leg cites the title stored on the Elasticsearch document."""
    r = _make_retriever()
    source = {
        "policy_id": "DOC-B",
        "title": "Document B Title",
        "version": "1.0",
        "jurisdiction": "REGION_B",
        "section_path": "Scope",
        "text": "Body.",
    }

    snippet = r._source_to_snippet(source, retrieval_path="rrf_only")

    assert snippet.title == "Document B Title"
