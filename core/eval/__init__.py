"""Evaluation dimension contracts and metric interfaces.

Defines the abstract contracts and Pydantic output schemas for the evaluation
dimensions. Concrete implementations live in eval/. Keeping contracts in core/
allows enforcement logic and audit to reference eval metadata without importing
the heavy evaluation harness.

Public API:
    EvalDimension:       Enum of the evaluation dimensions.
    DimensionResult:     Outcome of a single evaluation dimension run.
    EvalReport:          Aggregated report across the dimensions that ran.
    RetrievalMetrics:    Dimension 01 metric container.
    FaithfulnessMetrics: Dimension 02 metric container.
    ConsistencyMetrics:  Dimension 03 metric container.
    CitationMetrics:     Dimension 04 metric container.
    RobustnessMetrics:   Dimension 05 metric container.
    ShadowMetrics:       Shadow evaluation metric container (report-only).
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
    ShadowMetrics,
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
    "ShadowMetrics",
]
