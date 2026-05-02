"""Unit tests for the faithfulness dimension.

Stubs both the RAGAS scorer (the live RAGAS adapter is exercised in the
@pytest.mark.evaluation harness) and the LLMClient. Exercises:

- citation_rationale_overlap (pure)
- hallucination_rate (pure)
- Dimension wire-up: RAGAS + judge + overlap + hallucination compose into
  the typed metrics container; thresholds flag low scores; zero-tolerance
  hallucination fails on any case below the boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

import pytest
from pydantic import BaseModel

from core.eval.metrics import EvalDimension, FaithfulnessMetrics
from core.llm import CompletionResult, TokenUsage
from eval.dimensions.faithfulness import (
    HALLUCINATION_BOUNDARY,
    FaithfulnessCase,
    FaithfulnessDimension,
    citation_rationale_overlap,
    hallucination_rate,
)
from eval.judge import JudgePromptRegistry

if TYPE_CHECKING:
    from collections.abc import Sequence

T = TypeVar("T", bound=BaseModel)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubRagasScorer:
    """Returns canned RAGAS scores keyed by case_id (encoded in rationale)."""

    def __init__(self, scores_by_marker: dict[str, float]) -> None:
        self._scores = scores_by_marker
        self.calls: list[tuple[str, list[str]]] = []

    async def score(self, *, rationale: str, contexts: Sequence[str]) -> float:
        self.calls.append((rationale, list(contexts)))
        # Caller embeds a marker (e.g., "[c1]") in the rationale; we look it up.
        for marker, score in self._scores.items():
            if marker in rationale:
                return score
        return 0.0


class _StubLLMClient:
    """Returns canned ``CompletionResult[JudgeOutput]`` keyed by a marker in `user`."""

    def __init__(self, scores_by_marker: dict[str, float]) -> None:
        self._scores = scores_by_marker
        self.calls: list[str] = []

    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        response_format: type[T],
    ) -> CompletionResult[T]:
        del system  # judge type discriminated by template, not used in stub
        self.calls.append(user)
        score = 0.0
        for marker, s in self._scores.items():
            if marker in user:
                score = s
                break
        parsed = response_format.model_validate({"score": score, "reasoning": "stub"})
        return CompletionResult(
            parsed=parsed,
            usage=TokenUsage(
                prompt_tokens=len(user),
                completion_tokens=10,
                total_tokens=len(user) + 10,
                model="stub",
            ),
            latency_ms=0.0,
        )


# ---------------------------------------------------------------------------
# citation_rationale_overlap
# ---------------------------------------------------------------------------


class TestCitationRationaleOverlap:
    def test_full_overlap(self) -> None:
        score = citation_rationale_overlap(
            rationale="step up authentication required",
            cited_snippets=["step up authentication required"],
        )
        assert score == 1.0

    def test_disjoint(self) -> None:
        score = citation_rationale_overlap(
            rationale="alpha beta",
            cited_snippets=["gamma delta"],
        )
        assert score == 0.0

    def test_partial(self) -> None:
        # "step up auth" rationale + "step up required" cited
        # tokens:
        #   rationale = {step, up, auth}
        #   cited     = {step, up, required}
        # intersection = {step, up} = 2; union = {step, up, auth, required} = 4
        # overlap = 0.5
        score = citation_rationale_overlap(
            rationale="step up auth",
            cited_snippets=["step up required"],
        )
        assert score == pytest.approx(0.5)

    def test_case_insensitive(self) -> None:
        score = citation_rationale_overlap(
            rationale="STEP up AUTH",
            cited_snippets=["step UP auth"],
        )
        assert score == 1.0

    def test_empty_inputs(self) -> None:
        assert citation_rationale_overlap(rationale="", cited_snippets=[]) == 0.0


# ---------------------------------------------------------------------------
# hallucination_rate
# ---------------------------------------------------------------------------


class TestHallucinationRate:
    def test_no_hallucinations(self) -> None:
        assert hallucination_rate([0.9, 0.8, 0.7]) == 0.0

    def test_all_hallucinations(self) -> None:
        assert hallucination_rate([0.1, 0.2, 0.3]) == 1.0

    def test_partial(self) -> None:
        # Boundary is HALLUCINATION_BOUNDARY (0.5 default); strictly less.
        assert HALLUCINATION_BOUNDARY == 0.5
        assert hallucination_rate([0.4, 0.6, 0.49]) == pytest.approx(2 / 3)

    def test_boundary_not_flagged(self) -> None:
        # Score equal to the boundary is NOT a hallucination (uses <).
        assert hallucination_rate([0.5]) == 0.0

    def test_empty(self) -> None:
        assert hallucination_rate([]) == 0.0


# ---------------------------------------------------------------------------
# FaithfulnessDimension wire-up
# ---------------------------------------------------------------------------


def _case(case_id: str, *, rationale_marker: str | None = None) -> FaithfulnessCase:
    marker = rationale_marker or f"[{case_id}]"
    return FaithfulnessCase(
        case_id=case_id,
        rationale=f"{marker} step up authentication required",
        contexts=[f"{marker} ctx: step up authentication required"],
        cited_snippets=[f"{marker} step up authentication required"],
    )


class TestFaithfulnessDimension:
    @pytest.mark.asyncio
    async def test_high_quality_run_passes(self) -> None:
        cases = [_case("c1"), _case("c2")]
        ragas = _StubRagasScorer({"[c1]": 0.9, "[c2]": 0.92})
        judge = _StubLLMClient({"[c1]": 0.85, "[c2]": 0.9})
        dim = FaithfulnessDimension(
            ragas_scorer=ragas,
            judge_client=judge,
            cases=cases,
            prompt_registry=JudgePromptRegistry(),
        )
        run = await dim.evaluate()

        assert run.result.dimension == EvalDimension.FAITHFULNESS
        assert run.result.passed is True, run.result.threshold_violations
        assert isinstance(run.metrics, FaithfulnessMetrics)
        assert run.metrics.ragas_faithfulness == pytest.approx(0.91, abs=1e-3)
        assert run.metrics.llm_judge_grounding == pytest.approx(0.875)
        assert run.metrics.hallucination_rate == 0.0
        # Both stubs invoked once per case.
        assert len(ragas.calls) == 2
        assert len(judge.calls) == 2

    @pytest.mark.asyncio
    async def test_one_hallucination_fails_zero_tolerance(self) -> None:
        cases = [_case("c1"), _case("c2")]
        ragas = _StubRagasScorer({"[c1]": 0.9, "[c2]": 0.92})
        # c2 is below the hallucination boundary.
        judge = _StubLLMClient({"[c1]": 0.85, "[c2]": 0.3})
        dim = FaithfulnessDimension(ragas_scorer=ragas, judge_client=judge, cases=cases)
        run = await dim.evaluate()

        assert run.result.passed is False
        assert any("hallucination_rate" in v for v in run.result.threshold_violations)
        assert isinstance(run.metrics, FaithfulnessMetrics)
        assert run.metrics.hallucination_rate == 0.5

    @pytest.mark.asyncio
    async def test_low_ragas_fails(self) -> None:
        cases = [_case("c1")]
        ragas = _StubRagasScorer({"[c1]": 0.4})  # below 0.85 threshold
        judge = _StubLLMClient({"[c1]": 0.9})
        dim = FaithfulnessDimension(ragas_scorer=ragas, judge_client=judge, cases=cases)
        run = await dim.evaluate()

        assert run.result.passed is False
        assert any("ragas_faithfulness" in v for v in run.result.threshold_violations)

    @pytest.mark.asyncio
    async def test_empty_cases_does_not_pass(self) -> None:
        ragas = _StubRagasScorer({})
        judge = _StubLLMClient({})
        dim = FaithfulnessDimension(ragas_scorer=ragas, judge_client=judge, cases=[])
        run = await dim.evaluate()

        assert run.result.num_samples == 0
        assert run.result.passed is False
        # No upstream calls.
        assert ragas.calls == []
        assert judge.calls == []
