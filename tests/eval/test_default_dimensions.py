"""Tests for ``_build_default_dimensions()`` — the harness wiring layer.

Coverage scope:
- ``OPENAI_API_KEY`` fail-fast (no infrastructure required).
- All datasets missing → 5 ``SkippedDimension`` entries (heavy clients
  monkey-patched).

Live-stack dimension construction (real PolicyRetriever, OpenAI calls,
RAGAS init) is exercised by the Step 5 scenario smoke test against a
running Docker stack.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from app.settings import Settings
from core.eval.metrics import EvalDimension
from eval.dimensions.skipped import SkippedDimension
from eval.runners import harness

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# OPENAI_API_KEY fail-fast (no infrastructure or monkeypatching needed)
# ---------------------------------------------------------------------------


def test_raises_when_openai_api_key_missing():
    """Empty OPENAI_API_KEY raises ValueError before any client construction."""
    settings = Settings(openai_api_key="")
    with pytest.raises(ValueError, match="OPENAI_API_KEY is required"):
        harness._build_default_dimensions(settings=settings)


# ---------------------------------------------------------------------------
# All-missing-datasets path (heavy clients monkey-patched)
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_heavy_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub PipelineDriver, AsyncOpenAI, OpenAIJudgeClient, RagasFaithfulnessAdapter.

    Lets ``_build_default_dimensions`` run end-to-end without opening any
    Redis/PG/ES connections, instantiating an OpenAI client, or
    initializing RAGAS.
    """
    monkeypatch.setattr(harness, "PipelineDriver", MagicMock())
    monkeypatch.setattr(harness, "AsyncOpenAI", MagicMock())
    monkeypatch.setattr(harness, "OpenAIJudgeClient", MagicMock())
    monkeypatch.setattr(harness, "RagasFaithfulnessAdapter", MagicMock())


def test_all_datasets_missing_returns_five_skipped_dimensions(
    patch_heavy_clients,
    tmp_path: Path,
) -> None:
    """Empty dataset_root → all 5 dimensions become SkippedDimension."""
    settings = Settings(openai_api_key="sk-test")

    dimensions = harness._build_default_dimensions(
        settings=settings,
        dataset_root=tmp_path,
    )

    assert len(dimensions) == 5
    assert all(isinstance(d, SkippedDimension) for d in dimensions)


def test_all_datasets_missing_covers_each_canonical_kind(
    patch_heavy_clients,
    tmp_path: Path,
) -> None:
    """The 5 SkippedDimensions cover each EvalDimension exactly once."""
    settings = Settings(openai_api_key="sk-test")

    dimensions = harness._build_default_dimensions(
        settings=settings,
        dataset_root=tmp_path,
    )

    kinds = {d.kind for d in dimensions}
    assert kinds == {
        EvalDimension.RETRIEVAL,
        EvalDimension.FAITHFULNESS,
        EvalDimension.CONSISTENCY,
        EvalDimension.CITATION,
        EvalDimension.ROBUSTNESS,
    }


def test_skipped_dimensions_carry_dataset_path_in_reason(
    patch_heavy_clients,
    tmp_path: Path,
) -> None:
    """Each skipped dimension's reason names the missing dataset path."""
    settings = Settings(openai_api_key="sk-test")

    dimensions = harness._build_default_dimensions(
        settings=settings,
        dataset_root=tmp_path,
    )

    by_kind = {d.kind: d for d in dimensions}

    # Probe by evaluating each skipped dimension and reading the violation
    import asyncio

    retrieval_run = asyncio.run(by_kind[EvalDimension.RETRIEVAL].evaluate())
    assert "golden_queries.yaml" in retrieval_run.result.threshold_violations[0]

    faithfulness_run = asyncio.run(by_kind[EvalDimension.FAITHFULNESS].evaluate())
    assert "golden_outputs.yaml" in faithfulness_run.result.threshold_violations[0]

    consistency_run = asyncio.run(by_kind[EvalDimension.CONSISTENCY].evaluate())
    assert "scenarios" in consistency_run.result.threshold_violations[0]

    robustness_run = asyncio.run(by_kind[EvalDimension.ROBUSTNESS].evaluate())
    assert "robustness" in robustness_run.result.threshold_violations[0]
