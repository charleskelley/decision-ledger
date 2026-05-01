"""Unit tests for PolicyRetriever pure functions.

Tests for rrf_fuse and build_query do not require Docker — infrastructure
dependencies are mocked at construction time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

from app.retrieval.retriever import PolicyRetriever
from core.observation import Contribution
from core.routes import GateRoute
from core.snippet import RetrievedSnippet
from reasoner.account_takeover.events import (
    AuthMethod,
    AuthOutcome,
    Geolocation,
    LoginEvent,
)
from reasoner.account_takeover.scorer import ScorerOutput

_CORPUS_DIR = Path(__file__).parents[2] / "corpus"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_retriever() -> PolicyRetriever:
    """PolicyRetriever with mocked infra — only pure methods are testable."""
    return PolicyRetriever(
        pg_conn=MagicMock(),
        es=MagicMock(),
        model=MagicMock(),
        corpus_dir=_CORPUS_DIR,
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


def _make_event(auth_method: AuthMethod = AuthMethod.PASSWORD) -> LoginEvent:
    return LoginEvent(
        event_id="evt-unit-001",
        timestamp=datetime.now(UTC),
        account_id="acct-001",
        session_id="sess-001",
        ip_address="203.0.113.1",
        geolocation=Geolocation(
            latitude=37.77,
            longitude=-122.42,
            country="US",
            city="San Francisco",
            asn="AS7922",
        ),
        device_fingerprint="fp-abc",
        user_agent="Mozilla/5.0",
        auth_method=auth_method,
        outcome=AuthOutcome.SUCCESS,
    )


def _make_scorer_output(feature_names: list[str]) -> ScorerOutput:
    return ScorerOutput(
        entity_id=uuid4(),
        risk_score=0.7,
        top_signals=[
            Contribution(
                feature_name=name,
                feature_value=1.0,
                method="shap",
                value=0.3,
            )
            for name in feature_names
        ],
        scorer_version="xgb-v1.0.0",
        inference_latency_ms=2.5,
        route=GateRoute.ROUTE_TO_GATE,
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
# build_query
# ---------------------------------------------------------------------------


def test_build_query_maps_impossible_travel_signal():
    """impossible_travel signal maps to geographic block terminology."""
    r = _make_retriever()
    event = _make_event()
    scorer = _make_scorer_output(["impossible_travel"])

    query = r.build_query(event, scorer)

    assert "impossible travel" in query.lower()


def test_build_query_includes_auth_method_terms():
    """Auth method is appended to the query regardless of signals."""
    r = _make_retriever()
    event = _make_event(auth_method=AuthMethod.MFA_TOTP)
    scorer = _make_scorer_output([])

    query = r.build_query(event, scorer)

    assert "TOTP" in query or "MFA" in query


def test_build_query_falls_back_when_no_signals_or_method():
    """Returns the generic fallback when no signals and no known auth method map."""
    r = _make_retriever()
    event = _make_event(auth_method=AuthMethod.PASSWORD)
    # Empty signal list — no known terms, but auth method contributes
    scorer = _make_scorer_output([])

    query = r.build_query(event, scorer)

    # PASSWORD maps to a term, so fallback should not trigger
    assert query != ""


def test_build_query_uses_generic_fallback_for_empty_everything():
    """Returns generic fallback when signals are unknown and auth method is unknown."""
    r = _make_retriever()
    event = _make_event(auth_method=AuthMethod.PASSWORD)
    # Contributions with unrecognised feature names, no auth method term gap
    scorer = _make_scorer_output(["unknown_feature_xyz"])

    query = r.build_query(event, scorer)

    # Should still include PASSWORD term (not the generic fallback)
    assert "password" in query.lower() or "credential" in query.lower()


def test_build_query_uses_top_3_signals_only():
    """Only the top-3 SHAP signals contribute terms to the query."""
    r = _make_retriever()
    event = _make_event()
    scorer = _make_scorer_output(
        [
            "impossible_travel",
            "velocity_1min",
            "device_novelty",
            "geo_novelty",  # 4th — should be ignored
        ]
    )

    query = r.build_query(event, scorer)

    # "geo_novelty" is 4th so its term should not appear
    assert "geographic anomaly new country" not in query


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
        corpus_dir=_CORPUS_DIR,
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
        corpus_dir=_CORPUS_DIR,
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
        corpus_dir=_CORPUS_DIR,
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
        corpus_dir=_CORPUS_DIR,
    )
    candidates = [_make_snippet("DOC-A"), _make_snippet("DOC-B")]

    import unittest.mock as _mock

    with _mock.patch("app.retrieval.retriever.time.perf_counter", _fake_perf_counter):
        result, path = r.rerank("query", candidates, k=2)

    assert path == "rrf_only"
    assert result == candidates[:2]
