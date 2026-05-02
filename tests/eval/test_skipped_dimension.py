"""Tests for SkippedDimension — placeholder for unwired dimensions."""

from __future__ import annotations

import asyncio

from core.eval.metrics import EvalDimension
from eval.dimensions.skipped import SkippedDimension


def test_skipped_dimension_exposes_kind():
    """``kind`` property returns the constructor's value."""
    dim = SkippedDimension(
        kind=EvalDimension.FAITHFULNESS,
        reason="dataset missing: foo.yaml",
    )
    assert dim.kind == EvalDimension.FAITHFULNESS


def test_evaluate_returns_passed_false():
    """A skipped dimension counts as a failure for overall_passed."""
    dim = SkippedDimension(
        kind=EvalDimension.RETRIEVAL,
        reason="dataset missing",
    )
    run = asyncio.run(dim.evaluate())

    assert run.result.passed is False


def test_evaluate_surfaces_reason_in_threshold_violations():
    """The reason string appears in threshold_violations as 'skipped: <reason>'."""
    dim = SkippedDimension(
        kind=EvalDimension.CONSISTENCY,
        reason="no scenario files in eval/datasets/scenarios/",
    )
    run = asyncio.run(dim.evaluate())

    assert run.result.threshold_violations == [
        "skipped: no scenario files in eval/datasets/scenarios/"
    ]


def test_evaluate_returns_zero_samples():
    """num_samples is 0 — nothing was actually evaluated."""
    dim = SkippedDimension(
        kind=EvalDimension.CITATION,
        reason="dataset missing",
    )
    run = asyncio.run(dim.evaluate())

    assert run.result.num_samples == 0


def test_evaluate_returns_no_typed_metrics():
    """metrics is None — no typed block written to the EvalReport."""
    dim = SkippedDimension(
        kind=EvalDimension.ROBUSTNESS,
        reason="dataset missing",
    )
    run = asyncio.run(dim.evaluate())

    assert run.metrics is None


def test_evaluate_dimension_kind_matches_constructor():
    """The DimensionResult's dimension field carries the constructor kind."""
    dim = SkippedDimension(
        kind=EvalDimension.CITATION,
        reason="dataset missing",
    )
    run = asyncio.run(dim.evaluate())

    assert run.result.dimension == EvalDimension.CITATION
