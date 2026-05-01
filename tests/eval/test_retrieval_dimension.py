"""Unit tests for the retrieval dimension.

Tests pure metric functions and the dimension's wire-up against a stub
retriever. The integration test (live Postgres + Elasticsearch) lives under
``@pytest.mark.evaluation`` in the eval harness itself, not here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.eval.metrics import EvalDimension, RetrievalMetrics
from core.snippet import RetrievedSnippet
from eval.dimensions.retrieval import (
    GoldenQuery,
    RetrievalDimension,
    has_correct_top_version,
    jurisdictions_respected,
    load_golden_queries,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _snippet(
    *,
    document_id: str,
    version: str = "1.0",
    jurisdiction: str = "INTERNAL",
    section_path: str = "section",
) -> RetrievedSnippet:
    return RetrievedSnippet(
        document_id=document_id,
        title=document_id,
        version=version,
        jurisdiction=jurisdiction,
        section_path=section_path,
        text="text",
        relevance_score=0.5,
        retrieval_path="rrf_only",
    )


class _StubRetriever:
    """In-memory retriever — returns canned snippets per query."""

    def __init__(
        self,
        responses: dict[str, list[RetrievedSnippet]],
    ) -> None:
        self._responses = responses
        self.calls: list[tuple[str, int, list[str] | None, str | None]] = []

    def retrieve(
        self,
        query: str,
        k: int = 5,
        *,
        jurisdictions: list[str] | None = None,
        risk_tier: str | None = None,
    ) -> tuple[list[RetrievedSnippet], str]:
        self.calls.append((query, k, jurisdictions, risk_tier))
        return self._responses.get(query, []), "rrf_only"


# ---------------------------------------------------------------------------
# Pure metric functions
# ---------------------------------------------------------------------------


class TestPrecisionAtK:
    def test_all_relevant(self) -> None:
        assert precision_at_k(["A", "B"], {"A", "B"}, k=2) == 1.0

    def test_none_relevant(self) -> None:
        assert precision_at_k(["X", "Y"], {"A"}, k=2) == 0.0

    def test_partial(self) -> None:
        assert precision_at_k(["A", "X"], {"A"}, k=2) == 0.5

    def test_short_retrieved_counts_against(self) -> None:
        # 1 retrieved, 1 relevant, k=5 → 1/5 = 0.2
        assert precision_at_k(["A"], {"A"}, k=5) == pytest.approx(0.2)

    def test_zero_k(self) -> None:
        assert precision_at_k(["A"], {"A"}, k=0) == 0.0


class TestRecallAtK:
    def test_all_recovered(self) -> None:
        assert recall_at_k(["A", "B", "C"], {"A", "B"}, k=3) == 1.0

    def test_partial(self) -> None:
        assert recall_at_k(["A", "X"], {"A", "B"}, k=2) == 0.5

    def test_truncated_to_k(self) -> None:
        # B is relevant but at position 3 → not in top-2.
        assert recall_at_k(["A", "X", "B"], {"A", "B"}, k=2) == 0.5

    def test_no_relevant_is_vacuous(self) -> None:
        assert recall_at_k(["A"], set(), k=1) == 1.0


class TestReciprocalRank:
    def test_first_position(self) -> None:
        assert reciprocal_rank(["A", "B"], {"A"}) == 1.0

    def test_third_position(self) -> None:
        assert reciprocal_rank(["X", "Y", "A"], {"A"}) == pytest.approx(1 / 3)

    def test_no_relevant_returns_zero(self) -> None:
        assert reciprocal_rank(["X", "Y"], {"A"}) == 0.0

    def test_empty_returns_zero(self) -> None:
        assert reciprocal_rank([], {"A"}) == 0.0


class TestHasCorrectTopVersion:
    def test_match(self) -> None:
        snippets = [_snippet(document_id="DOC", version="2.0")]
        assert has_correct_top_version(
            snippets, expected_document_id="DOC", expected_version="2.0"
        )

    def test_wrong_doc(self) -> None:
        snippets = [_snippet(document_id="OTHER", version="2.0")]
        assert not has_correct_top_version(
            snippets, expected_document_id="DOC", expected_version="2.0"
        )

    def test_wrong_version(self) -> None:
        # Catches superseded version ranking #1.
        snippets = [_snippet(document_id="DOC", version="1.0")]
        assert not has_correct_top_version(
            snippets, expected_document_id="DOC", expected_version="2.0"
        )

    def test_empty(self) -> None:
        assert not has_correct_top_version(
            [], expected_document_id="DOC", expected_version="1.0"
        )


class TestJurisdictionsRespected:
    def test_all_in_jurisdiction(self) -> None:
        snippets = [
            _snippet(document_id="A", jurisdiction="INTERNAL"),
            _snippet(document_id="B", jurisdiction="INTERNAL"),
        ]
        assert jurisdictions_respected(snippets, ["INTERNAL"])

    def test_one_out_of_jurisdiction(self) -> None:
        snippets = [
            _snippet(document_id="A", jurisdiction="INTERNAL"),
            _snippet(document_id="B", jurisdiction="EU_GDPR"),
        ]
        assert not jurisdictions_respected(snippets, ["INTERNAL"])

    def test_no_filter_is_vacuous(self) -> None:
        snippets = [_snippet(document_id="A", jurisdiction="INTERNAL")]
        assert jurisdictions_respected(snippets, None)


# ---------------------------------------------------------------------------
# Golden query loading
# ---------------------------------------------------------------------------


class TestLoadGoldenQueries:
    def test_loads_default_dataset(self) -> None:
        from pathlib import Path

        path = (
            Path(__file__).parents[2]
            / "eval"
            / "datasets"
            / "retrieval"
            / "golden_queries.yaml"
        )
        queries = load_golden_queries(path)
        assert len(queries) >= 10
        assert all(isinstance(q, GoldenQuery) for q in queries)
        # Every query has at least one relevant doc.
        assert all(q.relevant_document_ids for q in queries)
        # Query IDs are unique.
        ids = [q.query_id for q in queries]
        assert len(ids) == len(set(ids))

    def test_round_trip_yaml(self, tmp_path: Path) -> None:
        fixture = tmp_path / "fixture.yaml"
        fixture.write_text(
            "queries:\n"
            "  - query_id: q01\n"
            "    query: 'find auth policy'\n"
            "    relevant_document_ids: ['DOC-A', 'DOC-B']\n"
            "    expected_top_document_id: DOC-A\n"
            "    expected_top_version: '1.0'\n",
            encoding="utf-8",
        )
        queries = load_golden_queries(fixture)
        assert len(queries) == 1
        assert queries[0].query_id == "q01"
        assert queries[0].relevant_document_ids == ["DOC-A", "DOC-B"]
        assert queries[0].jurisdictions is None


# ---------------------------------------------------------------------------
# RetrievalDimension — wire-up via stub retriever
# ---------------------------------------------------------------------------


class TestRetrievalDimension:
    @pytest.mark.asyncio
    async def test_perfect_run(self) -> None:
        # Two queries with multi-doc relevance; retriever returns all relevants
        # in the top positions plus one irrelevant filler per query.
        queries = [
            GoldenQuery(
                query_id="q1",
                query="auth policy",
                relevant_document_ids=["DOC-A", "DOC-B"],
                expected_top_document_id="DOC-A",
                expected_top_version="2.0",
            ),
            GoldenQuery(
                query_id="q2",
                query="velocity",
                jurisdictions=["INTERNAL"],
                relevant_document_ids=["DOC-V", "DOC-V2"],
                expected_top_document_id="DOC-V",
                expected_top_version="1.3",
            ),
        ]
        retriever = _StubRetriever(
            responses={
                "auth policy": [
                    _snippet(document_id="DOC-A", version="2.0"),
                    _snippet(document_id="DOC-B", version="1.0"),
                    _snippet(document_id="FILLER", version="1.0"),
                ],
                "velocity": [
                    _snippet(document_id="DOC-V", version="1.3"),
                    _snippet(document_id="DOC-V2", version="2.0"),
                    _snippet(document_id="FILLER", version="1.0"),
                ],
            }
        )
        dim = RetrievalDimension(retriever=retriever, golden_queries=queries, k=3)
        run = await dim.evaluate()

        assert run.result.dimension == EvalDimension.RETRIEVAL
        assert run.result.passed is True, run.result.threshold_violations
        assert run.result.threshold_violations == []
        assert run.result.num_samples == 2
        assert isinstance(run.metrics, RetrievalMetrics)
        assert run.metrics.version_resolution_accuracy == 1.0
        assert run.metrics.jurisdiction_filter_accuracy == 1.0
        assert run.metrics.mean_reciprocal_rank == 1.0
        # 2 of top-3 are relevant per query → precision@3 = 0.667.
        assert run.metrics.context_precision_at_k == pytest.approx(0.6667, abs=1e-3)

    @pytest.mark.asyncio
    async def test_superseded_version_fails_zero_tolerance(self) -> None:
        queries = [
            GoldenQuery(
                query_id="q1",
                query="auth risk",
                relevant_document_ids=["DOC-A"],
                expected_top_document_id="DOC-A",
                expected_top_version="2.0",
            ),
        ]
        # Retriever returns the right doc but at version 1.0 (superseded).
        retriever = _StubRetriever(
            responses={"auth risk": [_snippet(document_id="DOC-A", version="1.0")]}
        )
        dim = RetrievalDimension(retriever=retriever, golden_queries=queries, k=3)
        run = await dim.evaluate()

        assert run.result.passed is False
        assert any("version_resolution" in v for v in run.result.threshold_violations)
        assert isinstance(run.metrics, RetrievalMetrics)
        assert run.metrics.version_resolution_accuracy == 0.0

    @pytest.mark.asyncio
    async def test_jurisdiction_leak_fails(self) -> None:
        queries = [
            GoldenQuery(
                query_id="q1",
                query="gdpr",
                jurisdictions=["EU_GDPR"],
                relevant_document_ids=["GDPR-DOC"],
                expected_top_document_id="GDPR-DOC",
                expected_top_version="2018",
            ),
        ]
        # Retriever ignored the filter — returned an INTERNAL doc.
        retriever = _StubRetriever(
            responses={
                "gdpr": [
                    _snippet(
                        document_id="GDPR-DOC",
                        version="2018",
                        jurisdiction="EU_GDPR",
                    ),
                    _snippet(
                        document_id="LEAK",
                        version="1.0",
                        jurisdiction="INTERNAL",
                    ),
                ]
            }
        )
        dim = RetrievalDimension(retriever=retriever, golden_queries=queries, k=3)
        run = await dim.evaluate()

        assert run.result.passed is False
        assert any("jurisdiction_filter" in v for v in run.result.threshold_violations)

    @pytest.mark.asyncio
    async def test_threshold_violation_messages_human_readable(self) -> None:
        queries = [
            GoldenQuery(
                query_id="q1",
                query="x",
                relevant_document_ids=["DOC-A"],
                expected_top_document_id="DOC-A",
                expected_top_version="1.0",
            ),
        ]
        retriever = _StubRetriever(responses={"x": []})  # zero results
        dim = RetrievalDimension(retriever=retriever, golden_queries=queries, k=5)
        run = await dim.evaluate()

        assert run.result.passed is False
        # Several thresholds will fail; messages include numeric values.
        assert all(any(c.isdigit() for c in v) for v in run.result.threshold_violations)

    @pytest.mark.asyncio
    async def test_retriever_called_with_filters(self) -> None:
        queries = [
            GoldenQuery(
                query_id="q1",
                query="hva",
                jurisdictions=["INTERNAL"],
                risk_tier="HIGH_VALUE",
                relevant_document_ids=["DOC-H"],
                expected_top_document_id="DOC-H",
                expected_top_version="2.0",
            ),
        ]
        retriever = _StubRetriever(
            responses={"hva": [_snippet(document_id="DOC-H", version="2.0")]}
        )
        dim = RetrievalDimension(retriever=retriever, golden_queries=queries, k=3)
        await dim.evaluate()

        # Confirm filters were forwarded.
        assert retriever.calls == [("hva", 3, ["INTERNAL"], "HIGH_VALUE")]
