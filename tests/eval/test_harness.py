"""Unit tests for the eval harness runner.

Uses stub ``Dimension`` implementations so the harness wire-up is exercised
without invoking any LLM, retriever, or DB. Real dimensions ride on top of
this same protocol.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from core.eval.metrics import (
    CitationMetrics,
    ConsistencyMetrics,
    DimensionResult,
    EvalDimension,
    FaithfulnessMetrics,
    RetrievalMetrics,
    RobustnessMetrics,
)
from eval.dimensions import Dimension, DimensionRun
from eval.runners.harness import assemble_report, run_eval

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Stub dimensions
# ---------------------------------------------------------------------------


class _StubRetrievalDimension:
    kind = EvalDimension.RETRIEVAL

    def __init__(self, *, passed: bool = True, k: int = 5) -> None:
        self._passed = passed
        self._k = k

    async def evaluate(self) -> DimensionRun:
        result = DimensionResult(
            dimension=EvalDimension.RETRIEVAL,
            passed=self._passed,
            metrics={"context_precision_at_k": 0.85, "mean_reciprocal_rank": 0.72},
            threshold_violations=[] if self._passed else ["mrr below threshold"],
            evaluated_at=datetime.now(UTC),
            num_samples=30,
        )
        metrics = RetrievalMetrics(
            k=self._k,
            context_precision_at_k=0.85,
            context_recall_at_k=0.78,
            mean_reciprocal_rank=0.72,
            version_resolution_accuracy=1.0,
            jurisdiction_filter_accuracy=1.0,
        )
        return DimensionRun(result=result, metrics=metrics)


class _StubFaithfulnessDimension:
    kind = EvalDimension.FAITHFULNESS

    def __init__(self, *, passed: bool = True) -> None:
        self._passed = passed

    async def evaluate(self) -> DimensionRun:
        result = DimensionResult(
            dimension=EvalDimension.FAITHFULNESS,
            passed=self._passed,
            metrics={"ragas_faithfulness": 0.91},
            threshold_violations=[] if self._passed else ["hallucination detected"],
            evaluated_at=datetime.now(UTC),
            num_samples=8,
        )
        metrics = FaithfulnessMetrics(
            ragas_faithfulness=0.91,
            llm_judge_grounding=0.88,
            citation_rationale_overlap=0.74,
            hallucination_rate=0.0,
        )
        return DimensionRun(result=result, metrics=metrics)


class _StubConsistencyDimension:
    kind = EvalDimension.CONSISTENCY

    async def evaluate(self) -> DimensionRun:
        result = DimensionResult(
            dimension=EvalDimension.CONSISTENCY,
            passed=True,
            metrics={"action_stability_rate": 1.0},
            threshold_violations=[],
            evaluated_at=datetime.now(UTC),
            num_samples=24,
        )
        metrics = ConsistencyMetrics(
            num_scenarios=8,
            num_orderings=3,
            action_stability_rate=1.0,
            confidence_variance=0.02,
            rationale_semantic_similarity=0.91,
        )
        return DimensionRun(result=result, metrics=metrics)


class _StubCitationDimension:
    kind = EvalDimension.CITATION

    async def evaluate(self) -> DimensionRun:
        result = DimensionResult(
            dimension=EvalDimension.CITATION,
            passed=True,
            metrics={"citation_relevance_score": 0.83},
            threshold_violations=[],
            evaluated_at=datetime.now(UTC),
            num_samples=40,
        )
        metrics = CitationMetrics(
            citation_relevance_score=0.83,
            claim_citation_entailment=0.78,
            superficial_citation_rate=0.04,
        )
        return DimensionRun(result=result, metrics=metrics)


class _StubRobustnessDimension:
    kind = EvalDimension.ROBUSTNESS

    async def evaluate(self) -> DimensionRun:
        result = DimensionResult(
            dimension=EvalDimension.ROBUSTNESS,
            passed=True,
            metrics={"injection_resistance_rate": 1.0},
            threshold_violations=[],
            evaluated_at=datetime.now(UTC),
            num_samples=20,
        )
        metrics = RobustnessMetrics(
            injection_resistance_rate=1.0,
            schema_violation_handling=1.0,
            novel_pattern_action_accuracy=0.92,
            fallback_behavior_correctness=1.0,
        )
        return DimensionRun(result=result, metrics=metrics)


def _all_stub_dimensions() -> list[Dimension]:
    return [
        _StubRetrievalDimension(),
        _StubFaithfulnessDimension(),
        _StubConsistencyDimension(),
        _StubCitationDimension(),
        _StubRobustnessDimension(),
    ]


# ---------------------------------------------------------------------------
# assemble_report
# ---------------------------------------------------------------------------


class TestAssembleReport:
    @pytest.mark.asyncio
    async def test_populates_typed_metric_fields(self) -> None:
        runs = [await d.evaluate() for d in _all_stub_dimensions()]
        report = assemble_report(run_id="run-1", runs=runs)

        assert report.run_id == "run-1"
        assert report.overall_passed is True
        assert len(report.dimensions) == 5
        assert isinstance(report.retrieval, RetrievalMetrics)
        assert isinstance(report.faithfulness, FaithfulnessMetrics)
        assert isinstance(report.consistency, ConsistencyMetrics)
        assert isinstance(report.citation, CitationMetrics)
        assert isinstance(report.robustness, RobustnessMetrics)

    @pytest.mark.asyncio
    async def test_overall_passed_false_on_any_failure(self) -> None:
        retrieval_fail = _StubRetrievalDimension(passed=False)
        faithfulness_pass = _StubFaithfulnessDimension(passed=True)
        runs = [
            await retrieval_fail.evaluate(),
            await faithfulness_pass.evaluate(),
        ]
        report = assemble_report(run_id="run-2", runs=runs)

        assert report.overall_passed is False
        # Failed dimension is still in the list
        assert len(report.dimensions) == 2

    @pytest.mark.asyncio
    async def test_partial_run_unfilled_fields_remain_none(self) -> None:
        # Only retrieval ran — other metric fields stay None.
        runs = [await _StubRetrievalDimension().evaluate()]
        report = assemble_report(run_id="run-3", runs=runs)

        assert isinstance(report.retrieval, RetrievalMetrics)
        assert report.faithfulness is None
        assert report.consistency is None
        assert report.citation is None
        assert report.robustness is None

    def test_empty_runs_means_not_passed(self) -> None:
        # An empty harness run does NOT count as a pass — defensive default.
        report = assemble_report(run_id="run-empty", runs=[])
        assert report.overall_passed is False
        assert report.dimensions == []


# ---------------------------------------------------------------------------
# run_eval — end-to-end with stubs (no LLM, no DB)
# ---------------------------------------------------------------------------


class TestRunEval:
    @pytest.mark.asyncio
    async def test_writes_json_and_returns_report(self, tmp_path: Path) -> None:
        output_path = tmp_path / "report.json"
        report = await run_eval(
            dimensions=_all_stub_dimensions(),
            output_path=output_path,
        )

        assert report.overall_passed is True
        assert output_path.exists()

        # JSON content is the Pydantic-serialized report.
        import json

        loaded = json.loads(output_path.read_text(encoding="utf-8"))
        assert loaded["run_id"] == report.run_id
        assert loaded["overall_passed"] is True
        assert len(loaded["dimensions"]) == 5
        assert loaded["retrieval"]["k"] == 5
        assert loaded["faithfulness"]["hallucination_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_failed_dimension_propagates_to_report(self, tmp_path: Path) -> None:
        output_path = tmp_path / "report.json"
        dimensions: list[Dimension] = [
            _StubRetrievalDimension(passed=False),
            _StubFaithfulnessDimension(passed=True),
        ]
        report = await run_eval(dimensions=dimensions, output_path=output_path)

        assert report.overall_passed is False
        violations = {d.dimension: d.threshold_violations for d in report.dimensions}
        assert violations[EvalDimension.RETRIEVAL] == ["mrr below threshold"]
        assert violations[EvalDimension.FAITHFULNESS] == []
