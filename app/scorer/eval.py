"""Pure metric helpers for the ATO scorer training and evaluation pipeline.

These functions implement classical binary-classification metrics with numpy
only — no sklearn, no scipy. Intended for the small-to-medium synthetic
dataset the scorer trains on (~5000 samples). All functions are pure and
side-effect-free.

Public API:
    roc_auc                          ROC-AUC via Mann-Whitney U.
    log_loss                         Binary cross-entropy.
    precision_recall_at_threshold    P/R at a fixed threshold.
    confusion_matrix_at_threshold    [[TN, FP], [FN, TP]] at a threshold.
    routing_distribution             Fraction in each fast-path band.
    TrainingReport                   Pydantic schema for a training-run summary.
"""

from __future__ import annotations

from itertools import pairwise

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------


def roc_auc(y_true: np.ndarray, y_score: np.ndarray) -> float:
    """ROC-AUC via the rank-based Mann-Whitney U statistic.

    Handles ties by averaging ranks within tied groups, matching the standard
    ``sklearn.metrics.roc_auc_score`` behavior on tied scores.

    Args:
        y_true: 1-D array of binary labels (0 or 1).
        y_score: 1-D array of predicted probabilities.

    Returns:
        AUC in ``[0, 1]``. ``0.5`` is random; ``1.0`` is perfect.

    Raises:
        ValueError: If only one class is present (AUC is undefined).
    """
    y_true = np.asarray(y_true).astype(np.int64)
    y_score = np.asarray(y_score, dtype=np.float64)
    n_pos = int((y_true == 1).sum())
    n_neg = int((y_true == 0).sum())
    if n_pos == 0 or n_neg == 0:
        raise ValueError("AUC is undefined when only one class is present.")

    n = len(y_score)
    order = np.argsort(y_score, kind="mergesort")
    ranks = np.empty(n, dtype=np.float64)
    ranks[order] = np.arange(1, n + 1, dtype=np.float64)

    # Average ranks within tied score groups.
    sorted_scores = y_score[order]
    if n > 1:
        change_points = np.where(np.diff(sorted_scores) != 0)[0] + 1
        starts = np.concatenate(([0], change_points, [n]))
        for s, e in pairwise(starts):
            if e - s > 1:
                avg_rank = (s + 1 + e) / 2.0
                ranks[order[s:e]] = avg_rank

    pos_rank_sum = float(ranks[y_true == 1].sum())
    return (pos_rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def log_loss(y_true: np.ndarray, y_score: np.ndarray, *, eps: float = 1e-15) -> float:
    """Binary cross-entropy (negative log likelihood per sample).

    Clips predictions to ``[eps, 1 - eps]`` to avoid ``log(0)``.

    Args:
        y_true: 1-D array of binary labels (0 or 1).
        y_score: 1-D array of predicted probabilities in ``[0, 1]``.
        eps: Small floor/ceiling applied to ``y_score`` for numerical stability.

    Returns:
        Mean cross-entropy across samples.
    """
    y_true = np.asarray(y_true, dtype=np.float64)
    y_score = np.clip(np.asarray(y_score, dtype=np.float64), eps, 1.0 - eps)
    return float(
        -np.mean(y_true * np.log(y_score) + (1.0 - y_true) * np.log(1.0 - y_score))
    )


def precision_recall_at_threshold(
    y_true: np.ndarray, y_score: np.ndarray, threshold: float
) -> tuple[float, float]:
    """Precision and recall at a given decision threshold.

    Predictions are ``y_score >= threshold``. Both metrics return ``0.0``
    when their denominator is zero (no positives predicted; no positives in
    truth).

    Args:
        y_true: 1-D array of binary labels.
        y_score: 1-D array of predicted probabilities.
        threshold: Decision threshold.

    Returns:
        ``(precision, recall)`` in ``[0, 1]``.
    """
    y_true = np.asarray(y_true).astype(np.int64)
    y_pred = (np.asarray(y_score) >= threshold).astype(np.int64)
    tp = int(((y_pred == 1) & (y_true == 1)).sum())
    fp = int(((y_pred == 1) & (y_true == 0)).sum())
    fn = int(((y_pred == 0) & (y_true == 1)).sum())
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    return precision, recall


def confusion_matrix_at_threshold(
    y_true: np.ndarray, y_score: np.ndarray, threshold: float
) -> list[list[int]]:
    """Confusion matrix at a decision threshold.

    Args:
        y_true: 1-D array of binary labels.
        y_score: 1-D array of predicted probabilities.
        threshold: Decision threshold (predictions are ``y_score >= threshold``).

    Returns:
        ``[[TN, FP], [FN, TP]]`` — rows are truth, columns are predicted.
    """
    y_true = np.asarray(y_true).astype(np.int64)
    y_pred = (np.asarray(y_score) >= threshold).astype(np.int64)
    tn = int(((y_pred == 0) & (y_true == 0)).sum())
    fp = int(((y_pred == 1) & (y_true == 0)).sum())
    fn = int(((y_pred == 0) & (y_true == 1)).sum())
    tp = int(((y_pred == 1) & (y_true == 1)).sum())
    return [[tn, fp], [fn, tp]]


def routing_distribution(
    y_score: np.ndarray, *, allow_threshold: float, block_threshold: float
) -> dict[str, float]:
    """Fraction of scores landing in each fast-path band.

    Bands match ``app.scorer.scorer._route``: ``< allow_threshold`` →
    ``FAST_PATH_ALLOW``; ``> block_threshold`` → ``FAST_PATH_BLOCK``;
    otherwise → ``ROUTE_TO_GATE``.

    Args:
        y_score: 1-D array of predicted probabilities.
        allow_threshold: Lower band cutoff.
        block_threshold: Upper band cutoff.

    Returns:
        Dict with keys ``FAST_PATH_ALLOW``, ``ROUTE_TO_GATE``,
        ``FAST_PATH_BLOCK`` summing to ``1.0``. All-zeros on an empty input.
    """
    y_score = np.asarray(y_score, dtype=np.float64)
    n = len(y_score)
    if n == 0:
        return {
            "FAST_PATH_ALLOW": 0.0,
            "ROUTE_TO_GATE": 0.0,
            "FAST_PATH_BLOCK": 0.0,
        }
    n_allow = int((y_score < allow_threshold).sum())
    n_block = int((y_score > block_threshold).sum())
    n_gate = n - n_allow - n_block
    return {
        "FAST_PATH_ALLOW": n_allow / n,
        "ROUTE_TO_GATE": n_gate / n,
        "FAST_PATH_BLOCK": n_block / n,
    }


# ---------------------------------------------------------------------------
# TrainingReport
# ---------------------------------------------------------------------------


class TrainingReport(BaseModel):
    """Result of a single training run — train/test metrics and routing sanity.

    Captured by the trainer; included verbatim in the persisted ``ModelCard``.
    All probabilities and rates are in ``[0, 1]`` unless noted.

    Args:
        n_train: Number of samples in the training partition.
        n_test: Number of samples in the test partition.
        train_auc: ROC-AUC on the training set.
        test_auc: ROC-AUC on the test set.
        train_log_loss: Mean cross-entropy on the training set.
        test_log_loss: Mean cross-entropy on the test set.
        test_precision_at_0_5: Precision on the test set at threshold ``0.5``.
        test_recall_at_0_5: Recall on the test set at threshold ``0.5``.
        test_confusion_matrix: ``[[TN, FP], [FN, TP]]`` on the test set at 0.5.
        test_routing_distribution: Fraction of test predictions in each band.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    n_train: int = Field(ge=0)
    n_test: int = Field(ge=0)
    train_auc: float = Field(ge=0.0, le=1.0)
    test_auc: float = Field(ge=0.0, le=1.0)
    train_log_loss: float = Field(ge=0.0)
    test_log_loss: float = Field(ge=0.0)
    test_precision_at_0_5: float = Field(ge=0.0, le=1.0)
    test_recall_at_0_5: float = Field(ge=0.0, le=1.0)
    test_confusion_matrix: list[list[int]]
    test_routing_distribution: dict[str, float]


__all__ = [
    "TrainingReport",
    "confusion_matrix_at_threshold",
    "log_loss",
    "precision_recall_at_threshold",
    "roc_auc",
    "routing_distribution",
]
