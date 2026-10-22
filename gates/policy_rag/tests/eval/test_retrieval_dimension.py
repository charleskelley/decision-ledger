"""Unit tests for the retrieval dimension.

Tests pure metric functions and the dimension's wire-up: it drives the
gate's own retrieval (``retrieve_evidence``) over a reasoner's binding —
every corpus, through its provider, merged in precedence order — here with
a canned in-memory provider, and reports per-corpus metrics beside the
overall ones (DR-34). The live run (Postgres + Elasticsearch) is the
gate's eval harness itself (``make eval-gate-policy_rag``), not a unit test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from core.eval.metrics import EvalDimension, RetrievalMetrics
from policy_rag_gate.corpus import CorpusRef, CorpusState, PolicyBindingConfig
from policy_rag_gate.eval.retrieval import (
    GoldenQuery,
    RetrievalDimension,
    has_correct_top_version,
    jurisdictions_respected,
    load_golden_queries,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)
from policy_rag_gate.models import RetrievedContext, content_hash
from policy_rag_gate.providers import ProviderRegistry, ProviderResult

if TYPE_CHECKING:
    from collections.abc import Sequence
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
    corpus_id: str = "corpus-a",
    tier: int = 1,
    rank: int = 1,
) -> RetrievedContext:
    text = f"{corpus_id} {document_id} {version} {section_path} {rank}"
    return RetrievedContext(
        corpus_id=corpus_id,
        provider="reference",
        tier=tier,
        document_id=document_id,
        title=document_id,
        version=version,
        jurisdiction=jurisdiction,
        section_path=section_path,
        text=text,
        content_hash=content_hash(text),
        rank=rank,
        provenance_level="pinned_version",
    )


#: (document_id, version, jurisdiction) a canned provider returns for a query.
_Hit = tuple[str, str, str]


class _CannedProvider:
    """In-memory provider — canned hits per (corpus, query), in rank order."""

    provider_id = "reference"

    def __init__(
        self,
        responses: dict[tuple[str, str], list[_Hit]],
        *,
        applied: dict[str, str] | None = None,
    ) -> None:
        self._responses = responses
        self._applied = applied or {}
        self.calls: list[tuple[str, str, int, list[str] | None, str | None]] = []

    def describe(self, corpus: CorpusRef) -> CorpusState:
        return CorpusState(
            corpus_id=corpus.corpus_id,
            provider="reference",
            provider_version="canned",
            state_marker=None,
            provenance_level="pinned_version",
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
        self.calls.append(
            (
                query,
                corpus.corpus_id,
                k,
                None if jurisdictions is None else list(jurisdictions),
                risk_tier,
            )
        )
        hits = self._responses.get((corpus.corpus_id, query), [])[:k]
        return ProviderResult(
            state=self.describe(corpus),
            contexts=tuple(
                _snippet(
                    document_id=doc,
                    version=version,
                    jurisdiction=jurisdiction,
                    corpus_id=corpus.corpus_id,
                    tier=corpus.tier,
                    rank=rank,
                )
                for rank, (doc, version, jurisdiction) in enumerate(hits, 1)
            ),
            ranking_requested="reranked",
            ranking_applied=self._applied.get(corpus.corpus_id, "reranked"),
        )


def _binding(k: int = 3, *corpora: dict[str, Any]) -> PolicyBindingConfig:
    return PolicyBindingConfig.model_validate(
        {
            "corpora": list(corpora) or [{"corpus_id": "corpus-a", "tier": 1}],
            "k_per_corpus": k,
        }
    )


def _dimension(
    provider: _CannedProvider, queries: list[GoldenQuery], binding: PolicyBindingConfig
) -> RetrievalDimension:
    return RetrievalDimension(
        providers=ProviderRegistry.of(provider),  # type: ignore[arg-type]
        binding=binding,
        golden_queries=queries,
    )


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
# RetrievalDimension — wire-up via a canned provider
# ---------------------------------------------------------------------------


def _q(query_id: str, query: str, relevant: list[str], top: str, version: str, **kw):
    return GoldenQuery(
        query_id=query_id,
        query=query,
        relevant_document_ids=relevant,
        expected_top_document_id=top,
        expected_top_version=version,
        **kw,
    )


class TestRetrievalDimension:
    @pytest.mark.asyncio
    async def test_perfect_run(self) -> None:
        # Two queries with multi-doc relevance; the provider returns all
        # relevants in the top positions plus one irrelevant filler per query.
        queries = [
            _q("q1", "auth policy", ["DOC-A", "DOC-B"], "DOC-A", "2.0"),
            _q(
                "q2",
                "velocity",
                ["DOC-V", "DOC-V2"],
                "DOC-V",
                "1.3",
                jurisdictions=["INTERNAL"],
            ),
        ]
        provider = _CannedProvider(
            {
                ("corpus-a", "auth policy"): [
                    ("DOC-A", "2.0", "INTERNAL"),
                    ("DOC-B", "1.0", "INTERNAL"),
                    ("FILLER", "1.0", "INTERNAL"),
                ],
                ("corpus-a", "velocity"): [
                    ("DOC-V", "1.3", "INTERNAL"),
                    ("DOC-V2", "2.0", "INTERNAL"),
                    ("FILLER", "1.0", "INTERNAL"),
                ],
            }
        )
        run = await _dimension(provider, queries, _binding(3)).evaluate()

        assert run.result.dimension == EvalDimension.RETRIEVAL
        assert run.result.passed is True, run.result.threshold_violations
        assert run.result.threshold_violations == []
        assert run.result.num_samples == 2
        assert isinstance(run.metrics, RetrievalMetrics)
        assert run.metrics.k == 3
        assert run.metrics.version_resolution_accuracy == 1.0
        assert run.metrics.jurisdiction_filter_accuracy == 1.0
        assert run.metrics.mean_reciprocal_rank == 1.0
        # 2 of top-3 are relevant per query → precision@3 = 0.667.
        assert run.metrics.context_precision_at_k == pytest.approx(0.6667, abs=1e-3)
        # Per corpus, report-only.
        assert run.result.metrics["per_corpus.corpus-a.hit_rate"] == 1.0
        assert run.result.metrics["per_corpus.corpus-a.mean_returned"] == 3.0
        assert run.result.metrics["per_corpus.corpus-a.ranking_bypass_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_superseded_version_fails_zero_tolerance(self) -> None:
        queries = [_q("q1", "auth risk", ["DOC-A"], "DOC-A", "2.0")]
        # The provider returns the right doc but at version 1.0 (superseded).
        provider = _CannedProvider(
            {("corpus-a", "auth risk"): [("DOC-A", "1.0", "INTERNAL")]}
        )
        run = await _dimension(provider, queries, _binding(3)).evaluate()

        assert run.result.passed is False
        assert any("version_resolution" in v for v in run.result.threshold_violations)
        assert isinstance(run.metrics, RetrievalMetrics)
        assert run.metrics.version_resolution_accuracy == 0.0

    @pytest.mark.asyncio
    async def test_jurisdiction_leak_fails(self) -> None:
        queries = [
            _q(
                "q1",
                "gdpr",
                ["GDPR-DOC"],
                "GDPR-DOC",
                "2018",
                jurisdictions=["EU_GDPR"],
            )
        ]
        # The provider ignored the filter — returned an INTERNAL doc.
        provider = _CannedProvider(
            {
                ("corpus-a", "gdpr"): [
                    ("GDPR-DOC", "2018", "EU_GDPR"),
                    ("LEAK", "1.0", "INTERNAL"),
                ]
            }
        )
        run = await _dimension(provider, queries, _binding(3)).evaluate()

        assert run.result.passed is False
        assert any("jurisdiction_filter" in v for v in run.result.threshold_violations)

    @pytest.mark.asyncio
    async def test_threshold_violation_messages_human_readable(self) -> None:
        queries = [_q("q1", "x", ["DOC-A"], "DOC-A", "1.0")]
        provider = _CannedProvider({})  # zero results
        run = await _dimension(provider, queries, _binding(5)).evaluate()

        assert run.result.passed is False
        # Several thresholds will fail; messages include numeric values.
        assert all(any(c.isdigit() for c in v) for v in run.result.threshold_violations)

    @pytest.mark.asyncio
    async def test_every_bound_corpus_is_queried_with_the_filters(self) -> None:
        queries = [
            _q(
                "q1",
                "hva",
                ["DOC-H"],
                "DOC-H",
                "2.0",
                jurisdictions=["INTERNAL"],
                risk_tier="HIGH_VALUE",
            )
        ]
        provider = _CannedProvider(
            {("corpus-a", "hva"): [("DOC-H", "2.0", "INTERNAL")]}
        )
        binding = _binding(
            3,
            {"corpus_id": "corpus-a", "tier": 1},
            {"corpus_id": "corpus-b", "tier": 2},
        )
        await _dimension(provider, queries, binding).evaluate()

        # The quota and the filters reach each corpus, as in a decision.
        assert provider.calls == [
            ("hva", "corpus-a", 3, ["INTERNAL"], "HIGH_VALUE"),
            ("hva", "corpus-b", 3, ["INTERNAL"], "HIGH_VALUE"),
        ]

    @pytest.mark.asyncio
    async def test_two_corpora_report_per_corpus_metrics_and_the_overall_cutoff(
        self,
    ) -> None:
        queries = [
            _q("q1", "fresh device", ["REG-1", "PN-1"], "REG-1", "1.0"),
            _q("q2", "travel", ["REG-2"], "REG-2", "1.0"),
        ]
        provider = _CannedProvider(
            {
                ("regulatory", "fresh device"): [("REG-1", "1.0", "INTERNAL")],
                ("practice", "fresh device"): [
                    ("NOISE", "1.0", "INTERNAL"),
                    ("PN-1", "1.0", "INTERNAL"),
                ],
                ("regulatory", "travel"): [("REG-2", "1.0", "INTERNAL")],
                ("practice", "travel"): [("NOISE", "1.0", "INTERNAL")],
            },
            applied={"practice": "rrf_only"},
        )
        binding = _binding(
            2,
            {"corpus_id": "practice", "tier": 2},
            {"corpus_id": "regulatory", "tier": 1},
        )
        dim = _dimension(provider, queries, binding)

        run = await dim.evaluate()

        assert dim.k == 4  # two corpora, two passages each
        metrics = run.result.metrics
        assert metrics["per_corpus.regulatory.hit_rate"] == 1.0
        assert metrics["per_corpus.regulatory.mean_reciprocal_rank"] == 1.0
        assert metrics["per_corpus.practice.hit_rate"] == 0.5
        assert metrics["per_corpus.practice.mean_reciprocal_rank"] == 0.25
        assert metrics["per_corpus.practice.ranking_bypass_rate"] == 1.0
        assert metrics["per_corpus.regulatory.ranking_bypass_rate"] == 0.0
        # The overall ranking is the rendered order: tier 1 first.
        assert run.metrics.mean_reciprocal_rank == 1.0  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_a_corpus_the_filter_excludes_has_no_per_corpus_samples(
        self,
    ) -> None:
        queries = [_q("q1", "gdpr", ["GDPR"], "GDPR", "1", jurisdictions=["EU_GDPR"])]
        provider = _CannedProvider({("eu", "gdpr"): [("GDPR", "1", "EU_GDPR")]})
        binding = _binding(
            2,
            {"corpus_id": "eu", "tier": 1},
            {"corpus_id": "us-only", "tier": 2, "jurisdictions": ["US_FEDERAL"]},
        )

        run = await _dimension(provider, queries, binding).evaluate()

        assert [call[1] for call in provider.calls] == ["eu"]
        assert "per_corpus.us-only.hit_rate" not in run.result.metrics
        assert run.result.metrics["per_corpus.eu.hit_rate"] == 1.0
