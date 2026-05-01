"""Dimension contract — the Protocol every concrete dimension implements.

Each of the 5 dimensions (retrieval, faithfulness, consistency, citation,
robustness) implements ``Dimension`` and returns a ``DimensionRun`` carrying
both a generic ``DimensionResult`` (dimension/passed/metrics-dict/violations)
and the typed metric container (``RetrievalMetrics``, ``FaithfulnessMetrics``,
etc.) the eval report stores in its typed fields.

Dimensions take their dependencies (golden datasets, retriever, gate, judge
clients) at construction time. ``evaluate()`` runs without further inputs —
the harness just calls it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from core.eval.metrics import (
    CitationMetrics,
    ConsistencyMetrics,
    DimensionResult,
    EvalDimension,
    FaithfulnessMetrics,
    RetrievalMetrics,
    RobustnessMetrics,
)

DimensionMetrics = (
    RetrievalMetrics
    | FaithfulnessMetrics
    | ConsistencyMetrics
    | CitationMetrics
    | RobustnessMetrics
)


@dataclass(frozen=True)
class DimensionRun:
    """Output of one dimension's ``evaluate()`` call.

    Args:
        result: Generic per-dimension outcome — dimension kind, pass/fail,
            metrics dict, threshold violations, sample count.
        metrics: Typed metric container the ``EvalReport`` stores in its
            kind-specific field. ``None`` is allowed only when the dimension
            ran but produced no scoreable samples (e.g., empty dataset);
            normal runs always populate this.
    """

    result: DimensionResult
    metrics: DimensionMetrics | None = None


class Dimension(Protocol):
    """A single eval dimension.

    Implementations are constructed with their dependencies (datasets,
    pipeline clients, judge clients) and expose an async ``evaluate()`` the
    runner calls.
    """

    kind: EvalDimension

    async def evaluate(self) -> DimensionRun:
        """Run the dimension end-to-end and return its ``DimensionRun``."""
        ...
