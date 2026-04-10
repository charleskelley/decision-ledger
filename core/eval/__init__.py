"""Evaluation dimension contracts and metric interfaces.

Defines the abstract contracts and Pydantic output schemas for all 5 evaluation
dimensions. Concrete implementations live in eval/. Keeping contracts in core/
allows enforcement logic and audit to reference eval metadata without importing
the heavy evaluation harness.

Public API:
    EvalDimension:       Enum of the five evaluation dimensions.
    DimensionResult:     Outcome of a single evaluation dimension run.
    EvalReport:          Aggregated report across all 5 dimensions.
    RetrievalMetrics:    Dimension 01 metric container.
    FaithfulnessMetrics: Dimension 02 metric container.
    ConsistencyMetrics:  Dimension 03 metric container.
    CitationMetrics:     Dimension 04 metric container.
    RobustnessMetrics:   Dimension 05 metric container.
"""

from core.eval.metrics import (
    CitationMetrics,
    ConsistencyMetrics,
    DimensionResult,
    EvalDimension,
    EvalReport,
    FaithfulnessMetrics,
    RetrievalMetrics,
    RobustnessMetrics,
)

__all__ = [
    "CitationMetrics",
    "ConsistencyMetrics",
    "DimensionResult",
    "EvalDimension",
    "EvalReport",
    "FaithfulnessMetrics",
    "RetrievalMetrics",
    "RobustnessMetrics",
]
