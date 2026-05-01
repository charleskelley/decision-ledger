"""Unit tests for the pure metric helpers in app/scorer/eval.py."""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError

from app.scorer.eval import (
    TrainingReport,
    confusion_matrix_at_threshold,
    log_loss,
    precision_recall_at_threshold,
    roc_auc,
    routing_distribution,
)

# ---------------------------------------------------------------------------
# roc_auc
# ---------------------------------------------------------------------------


class TestRocAuc:
    def test_perfect_classifier(self) -> None:
        y_true = np.array([0, 0, 1, 1])
        y_score = np.array([0.1, 0.2, 0.8, 0.9])
        assert roc_auc(y_true, y_score) == 1.0

    def test_inverse_classifier(self) -> None:
        y_true = np.array([0, 0, 1, 1])
        y_score = np.array([0.9, 0.8, 0.2, 0.1])
        assert roc_auc(y_true, y_score) == 0.0

    def test_random_classifier(self) -> None:
        # Identical scores → all ranks tie → AUC = 0.5.
        y_true = np.array([0, 1, 0, 1])
        y_score = np.array([0.5, 0.5, 0.5, 0.5])
        assert roc_auc(y_true, y_score) == 0.5

    def test_partial_overlap(self) -> None:
        # 3 positives, 3 negatives, scores partly intermingled.
        y_true = np.array([0, 0, 1, 0, 1, 1])
        y_score = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
        # Pos ranks: 3, 5, 6 → sum = 14. n_pos*(n_pos+1)/2 = 6.
        # AUC = (14 - 6) / (3 * 3) = 8/9 ≈ 0.889
        assert roc_auc(y_true, y_score) == pytest.approx(8.0 / 9.0)

    def test_raises_when_only_one_class(self) -> None:
        with pytest.raises(ValueError, match="only one class"):
            roc_auc(np.array([0, 0, 0]), np.array([0.1, 0.2, 0.3]))

    def test_handles_ties_with_average_rank(self) -> None:
        # Tied middle group: 2 zeros and 2 ones at the same score should
        # average to AUC ≈ 0.5 contribution.
        y_true = np.array([0, 1, 0, 1])
        y_score = np.array([0.0, 0.5, 0.5, 1.0])
        # Pos at rank 4 plus tied group of 2 (ranks 2,3 → avg 2.5).
        # Pos rank sum = 4 + 2.5 = 6.5; n_pos=2 ⇒ 6.5 - 3 = 3.5; n_neg=2 ⇒ 3.5/4 = 0.875
        assert roc_auc(y_true, y_score) == pytest.approx(0.875)


# ---------------------------------------------------------------------------
# log_loss
# ---------------------------------------------------------------------------


class TestLogLoss:
    def test_perfect_predictions_near_zero(self) -> None:
        y_true = np.array([0, 1])
        y_score = np.array([0.001, 0.999])
        assert log_loss(y_true, y_score) < 0.01

    def test_random_half_predictions(self) -> None:
        y_true = np.array([0, 0, 1, 1])
        y_score = np.array([0.5, 0.5, 0.5, 0.5])
        assert log_loss(y_true, y_score) == pytest.approx(np.log(2.0))

    def test_clip_avoids_log_zero(self) -> None:
        # Score of 0.0 with positive label would be -inf without clipping.
        y_true = np.array([1])
        y_score = np.array([0.0])
        # Should be a large but finite value.
        result = log_loss(y_true, y_score)
        assert np.isfinite(result)
        assert result > 30  # log(1e-15) ≈ 34.5

    def test_log_loss_non_negative(self) -> None:
        y_true = np.array([0, 1])
        y_score = np.array([0.3, 0.7])
        assert log_loss(y_true, y_score) > 0


# ---------------------------------------------------------------------------
# precision_recall_at_threshold
# ---------------------------------------------------------------------------


class TestPrecisionRecallAtThreshold:
    def test_perfect(self) -> None:
        y_true = np.array([0, 0, 1, 1])
        y_score = np.array([0.1, 0.2, 0.8, 0.9])
        precision, recall = precision_recall_at_threshold(y_true, y_score, 0.5)
        assert precision == 1.0
        assert recall == 1.0

    def test_no_positives_predicted(self) -> None:
        y_true = np.array([0, 1])
        y_score = np.array([0.1, 0.2])
        precision, recall = precision_recall_at_threshold(y_true, y_score, 0.5)
        assert precision == 0.0  # zero TP+FP → 0 by convention
        assert recall == 0.0

    def test_partial(self) -> None:
        y_true = np.array([0, 0, 1, 1])
        y_score = np.array([0.4, 0.6, 0.6, 0.4])  # 2 predicted pos: 1 right, 1 wrong
        precision, recall = precision_recall_at_threshold(y_true, y_score, 0.5)
        assert precision == 0.5
        assert recall == 0.5


# ---------------------------------------------------------------------------
# confusion_matrix_at_threshold
# ---------------------------------------------------------------------------


class TestConfusionMatrix:
    def test_shape_and_total(self) -> None:
        y_true = np.array([0, 0, 1, 1, 1])
        y_score = np.array([0.1, 0.6, 0.7, 0.4, 0.9])
        cm = confusion_matrix_at_threshold(y_true, y_score, 0.5)
        assert len(cm) == 2
        assert all(len(row) == 2 for row in cm)
        # Total cells must equal n_samples.
        assert sum(cell for row in cm for cell in row) == 5

    def test_perfect_classifier(self) -> None:
        y_true = np.array([0, 0, 1, 1])
        y_score = np.array([0.1, 0.2, 0.8, 0.9])
        cm = confusion_matrix_at_threshold(y_true, y_score, 0.5)
        # [[TN, FP], [FN, TP]]
        assert cm == [[2, 0], [0, 2]]

    def test_all_wrong(self) -> None:
        y_true = np.array([0, 0, 1, 1])
        y_score = np.array([0.9, 0.8, 0.1, 0.2])
        cm = confusion_matrix_at_threshold(y_true, y_score, 0.5)
        assert cm == [[0, 2], [2, 0]]


# ---------------------------------------------------------------------------
# routing_distribution
# ---------------------------------------------------------------------------


class TestRoutingDistribution:
    def test_sums_to_one(self) -> None:
        scores = np.array([0.05, 0.3, 0.5, 0.7, 0.95])
        dist = routing_distribution(scores, allow_threshold=0.20, block_threshold=0.85)
        assert sum(dist.values()) == pytest.approx(1.0)

    def test_partition_matches_thresholds(self) -> None:
        # 0.05 < 0.20 → ALLOW; 0.3, 0.5, 0.7 in band → GATE; 0.95 > 0.85 → BLOCK.
        scores = np.array([0.05, 0.3, 0.5, 0.7, 0.95])
        dist = routing_distribution(scores, allow_threshold=0.20, block_threshold=0.85)
        assert dist["FAST_PATH_ALLOW"] == pytest.approx(0.2)
        assert dist["ROUTE_TO_GATE"] == pytest.approx(0.6)
        assert dist["FAST_PATH_BLOCK"] == pytest.approx(0.2)

    def test_empty_input_zeros(self) -> None:
        dist = routing_distribution(
            np.array([]), allow_threshold=0.20, block_threshold=0.85
        )
        assert dist == {
            "FAST_PATH_ALLOW": 0.0,
            "ROUTE_TO_GATE": 0.0,
            "FAST_PATH_BLOCK": 0.0,
        }

    def test_boundary_values_route_to_gate(self) -> None:
        # Score equal to a threshold: ALLOW uses strict <, BLOCK uses strict >;
        # so threshold-exact scores fall into ROUTE_TO_GATE.
        scores = np.array([0.20, 0.85])
        dist = routing_distribution(scores, allow_threshold=0.20, block_threshold=0.85)
        assert dist["ROUTE_TO_GATE"] == 1.0


# ---------------------------------------------------------------------------
# TrainingReport schema
# ---------------------------------------------------------------------------


class TestTrainingReport:
    def test_valid_construction(self) -> None:
        report = TrainingReport(
            n_train=80,
            n_test=20,
            train_auc=0.95,
            test_auc=0.92,
            train_log_loss=0.2,
            test_log_loss=0.25,
            test_precision_at_0_5=0.88,
            test_recall_at_0_5=0.85,
            test_confusion_matrix=[[10, 2], [1, 7]],
            test_routing_distribution={
                "FAST_PATH_ALLOW": 0.5,
                "ROUTE_TO_GATE": 0.3,
                "FAST_PATH_BLOCK": 0.2,
            },
        )
        assert report.n_train == 80
        assert report.test_auc == 0.92

    def test_auc_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TrainingReport(
                n_train=80,
                n_test=20,
                train_auc=1.5,
                test_auc=0.92,
                train_log_loss=0.2,
                test_log_loss=0.25,
                test_precision_at_0_5=0.88,
                test_recall_at_0_5=0.85,
                test_confusion_matrix=[[10, 2], [1, 7]],
                test_routing_distribution={},
            )

    def test_frozen(self) -> None:
        report = TrainingReport(
            n_train=80,
            n_test=20,
            train_auc=0.95,
            test_auc=0.92,
            train_log_loss=0.2,
            test_log_loss=0.25,
            test_precision_at_0_5=0.88,
            test_recall_at_0_5=0.85,
            test_confusion_matrix=[[10, 2], [1, 7]],
            test_routing_distribution={"FAST_PATH_ALLOW": 1.0},
        )
        with pytest.raises(ValidationError):
            report.test_auc = 0.0
