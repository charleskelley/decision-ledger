"""Unit tests for PolicyRetriever pure functions and row mapping.

The retriever is the reference provider's engine (DR-34). Tests for
rrf_fuse, rerank, and the row/source → chunk conversions do not require
Docker — infrastructure dependencies are mocked at construction time. The
retriever reads no corpus files: every chunk row carries its document
``title`` and its deterministic ``chunk_id``, and both stores are filtered by
``corpus_id``. Domain-specific query construction lives with each reasoner
(``reasoner/<domain>/retrieval_query.py``) and is tested there.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

from policy_rag_gate.retriever import PolicyRetriever, ScoredChunk

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


def _make_chunk(
    document_id: str,
    section_path: str = "Overview",
    *,
    relevance_score: float = 0.5,
) -> ScoredChunk:
    return ScoredChunk(
        chunk_id=f"{document_id}/{section_path}",
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
    shared = _make_chunk("SHARED-DOC")
    dense_only = _make_chunk("DENSE-ONLY")
    sparse_only = _make_chunk("SPARSE-ONLY")

    fused = r.rrf_fuse([shared, dense_only], [shared, sparse_only], k=3)

    scores = {s.document_id: s.relevance_score for s in fused}
    assert scores["SHARED-DOC"] > scores["DENSE-ONLY"]
    assert scores["SHARED-DOC"] > scores["SPARSE-ONLY"]


def test_rrf_fuse_deduplicates_same_snippet():
    """The same chunk from both lists appears exactly once in the output."""
    r = _make_retriever()
    snippet = _make_chunk("DOC-A")

    fused = r.rrf_fuse([snippet], [snippet], k=5)

    assert len(fused) == 1
    assert fused[0].document_id == "DOC-A"


def test_rrf_fuse_truncates_to_k():
    """Output is capped at k results regardless of candidate pool size."""
    r = _make_retriever()
    dense = [_make_chunk(f"D-{i}") for i in range(5)]
    sparse = [_make_chunk(f"S-{i}") for i in range(5)]

    fused = r.rrf_fuse(dense, sparse, k=3)

    assert len(fused) == 3


def test_rrf_fuse_updates_relevance_score_to_rrf_value():
    """rrf_fuse overwrites the original relevance_score with the RRF score."""
    r = _make_retriever()
    snippet = _make_chunk("DOC-A", relevance_score=0.99)

    fused = r.rrf_fuse([snippet], [], k=1)

    # RRF score for rank-1 with k=60 is 1/61 ≈ 0.016
    assert fused[0].relevance_score < 0.1


def test_rrf_fuse_deduplicates_by_full_snippet_key():
    """Same policy_id but different section_path — these are distinct chunks."""
    r = _make_retriever()
    a = _make_chunk("DOC-A", section_path="Section 1")
    b = _make_chunk("DOC-A", section_path="Section 2")

    fused = r.rrf_fuse([a], [b], k=5)

    assert len(fused) == 2


def test_rrf_fuse_keeps_distinct_chunks_that_share_a_section_path():
    """Two chunks of one section (the loader suffixes their ids) stay two.

    Fusion is keyed on the chunk id, so the better-ranked chunk keeps its
    own text and score instead of being merged into its sibling.
    """
    r = _make_retriever()
    first = dataclasses.replace(_make_chunk("DOC-A"), chunk_id="a", text="First.")
    second = dataclasses.replace(_make_chunk("DOC-A"), chunk_id="a#2", text="Second.")

    fused = r.rrf_fuse([first, second], [second], k=5)

    assert [(c.chunk_id, c.text) for c in fused] == [
        ("a#2", "Second."),
        ("a", "First."),
    ]


# ---------------------------------------------------------------------------
# rerank
# ---------------------------------------------------------------------------


def test_rerank_returns_rrf_only_when_no_cross_encoder():
    """Without a cross-encoder, rerank returns top-k RRF results unchanged."""
    r = _make_retriever()
    candidates = [_make_chunk(f"DOC-{i}") for i in range(5)]

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
        _make_chunk("DOC-A"),
        _make_chunk("DOC-B"),
        _make_chunk("DOC-C"),
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
    candidates = [_make_chunk("DOC-A", relevance_score=0.3)]

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
    candidates = [_make_chunk(f"DOC-{i}") for i in range(5)]

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
    candidates = [_make_chunk("DOC-A"), _make_chunk("DOC-B")]

    import unittest.mock as _mock

    with _mock.patch("policy_rag_gate.retriever.time.perf_counter", _fake_perf_counter):
        result, path = r.rerank("query", candidates, k=2)

    assert path == "rrf_only"
    assert result == candidates[:2]


# ---------------------------------------------------------------------------
# Row / source → chunk mapping (title comes from the stores, not the corpus)
# ---------------------------------------------------------------------------


def test_row_to_chunk_reads_title_id_and_span_from_the_row():
    """The dense leg cites the title stored on the chunk row."""
    r = _make_retriever()
    row = {
        "chunk_id": "a" * 64,
        "span_start": 12,
        "span_end": 17,
        "policy_id": "DOC-A",
        "policy_version": "2.0",
        "title": "Document A Title",
        "jurisdiction": "REGION_A",
        "section_path": "Overview",
        "chunk_text": "Body.",
        "relevance_score": 0.42,
    }

    chunk = r._row_to_chunk(row, retrieval_path="rrf_only")

    assert chunk.title == "Document A Title"
    assert chunk.document_id == "DOC-A"
    assert chunk.relevance_score == 0.42
    assert chunk.chunk_id == "a" * 64
    assert chunk.span == (12, 17)


def test_row_to_chunk_falls_back_to_document_id_for_a_blank_title():
    """A chunk row with a blank title cites its document id."""
    r = _make_retriever()
    row = {
        "chunk_id": "b" * 64,
        "span_start": None,
        "span_end": None,
        "policy_id": "DOC-OLD",
        "policy_version": "1.0",
        "title": "",
        "jurisdiction": "REGION_A",
        "section_path": "Overview",
        "chunk_text": "Body.",
        "relevance_score": 0.1,
    }

    chunk = r._row_to_chunk(row, retrieval_path="rrf_only")

    assert chunk.title == "DOC-OLD"
    assert chunk.span is None


def test_dense_search_selects_title_column_and_filters_by_corpus():
    """The dense SQL reads title from Postgres and scopes by corpus_id."""
    pg = MagicMock()
    cursor = pg.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [
        {
            "chunk_id": "c" * 64,
            "span_start": 0,
            "span_end": 5,
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

    chunks = r.dense_search("query", k=1, corpus_id="some-corpus")

    sql, params = cursor.execute.call_args.args
    assert "title" in sql.split("FROM")[0]
    assert "chunk_id" in sql.split("FROM")[0]
    assert "corpus_id = %s" in sql
    assert "reasoner_id" not in sql
    assert "some-corpus" in params
    assert chunks[0].title == "Document A Title"


def test_sparse_search_filters_by_corpus():
    """The BM25 leg filters the shared index by the corpus_id keyword."""
    es = MagicMock()
    es.search.return_value = {"hits": {"hits": []}}
    r = PolicyRetriever(pg_conn=MagicMock(), es=es, model=MagicMock())

    r.sparse_search("query", 4, corpus_id="some-corpus", jurisdictions=["REGION_A"])

    query = es.search.call_args.kwargs["query"]
    assert {"term": {"corpus_id": "some-corpus"}} in query["bool"]["filter"]
    assert {"terms": {"jurisdiction": ["REGION_A"]}} in query["bool"]["filter"]


def test_source_to_chunk_reads_title_from_the_es_document():
    """The sparse leg cites the title stored on the Elasticsearch document."""
    r = _make_retriever()
    source = {
        "chunk_id": "d" * 64,
        "span_start": 3,
        "span_end": 8,
        "policy_id": "DOC-B",
        "title": "Document B Title",
        "version": "1.0",
        "jurisdiction": "REGION_B",
        "section_path": "Scope",
        "text": "Body.",
    }

    chunk = r._source_to_chunk(source, retrieval_path="rrf_only")

    assert chunk.title == "Document B Title"
    assert chunk.chunk_id == "d" * 64
    assert chunk.span == (3, 8)
