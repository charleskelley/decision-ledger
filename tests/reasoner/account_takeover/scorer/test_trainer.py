"""Unit tests for the ATO scorer training pipeline.

The training pipeline is small enough to test end-to-end with a tiny
dataset (200 samples, 30 estimators) — runs in well under a second on a
laptop. Tests cover:

  - Train/test split disjointness and seed determinism.
  - Heuristic-label distribution non-degenerate.
  - `train()` produces a populated ``ModelCard`` with required fields.
  - Training meets a sanity floor (test_auc > 0.9 on synthetic data).
  - Model-card SHA-256 matches the binary on disk.
  - Train/test CSVs are written when ``persist_data=True``.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import numpy as np

from reasoner.account_takeover.scorer.eval import TrainingReport
from reasoner.account_takeover.scorer.model_card import ModelCard
from reasoner.account_takeover.scorer.scorer import FEATURE_NAMES
from reasoner.account_takeover.scorer.trainer import (
    HEURISTIC_LABEL_VERSION,
    _generate_dataset,
    _train_test_split,
    train,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# _train_test_split
# ---------------------------------------------------------------------------


class TestTrainTestSplit:
    def test_disjoint(self) -> None:
        x, y = _generate_dataset(200)
        (x_train, _), (x_test, _) = _train_test_split(x, y)
        assert len(x_train) + len(x_test) == 200
        # No overlap by row identity (use byte representation).
        train_rows = {row.tobytes() for row in x_train}
        test_rows = {row.tobytes() for row in x_test}
        assert train_rows.isdisjoint(test_rows)

    def test_default_80_20(self) -> None:
        x, y = _generate_dataset(100)
        (x_train, _), (x_test, _) = _train_test_split(x, y)
        assert len(x_test) == 20
        assert len(x_train) == 80

    def test_deterministic(self) -> None:
        x, y = _generate_dataset(200)
        (x_train_a, y_train_a), (x_test_a, y_test_a) = _train_test_split(x, y, seed=7)
        (x_train_b, y_train_b), (x_test_b, y_test_b) = _train_test_split(x, y, seed=7)
        np.testing.assert_array_equal(x_train_a, x_train_b)
        np.testing.assert_array_equal(y_train_a, y_train_b)
        np.testing.assert_array_equal(x_test_a, x_test_b)
        np.testing.assert_array_equal(y_test_a, y_test_b)

    def test_different_seeds_diverge(self) -> None:
        x, y = _generate_dataset(200)
        (_, _), (_, y_test_a) = _train_test_split(x, y, seed=1)
        (_, _), (_, y_test_b) = _train_test_split(x, y, seed=2)
        # Different seeds shuffle differently — vanishingly unlikely the two
        # 40-element label sequences coincide by chance.
        assert not np.array_equal(y_test_a, y_test_b)


# ---------------------------------------------------------------------------
# _generate_dataset — label distribution
# ---------------------------------------------------------------------------


class TestDatasetGeneration:
    def test_label_distribution_non_degenerate(self) -> None:
        _, y = _generate_dataset(500)
        n_pos = float((y == 1).sum())
        n_neg = float((y == 0).sum())
        # Both classes present with at least 5% each.
        assert n_pos / len(y) > 0.05
        assert n_neg / len(y) > 0.05

    def test_feature_columns_match_names(self) -> None:
        x, _ = _generate_dataset(50)
        assert x.shape[1] == len(FEATURE_NAMES)

    def test_seed_deterministic(self) -> None:
        x_a, y_a = _generate_dataset(100)
        x_b, y_b = _generate_dataset(100)
        np.testing.assert_array_equal(x_a, x_b)
        np.testing.assert_array_equal(y_a, y_b)


# ---------------------------------------------------------------------------
# train() — end-to-end with tmp_path
# ---------------------------------------------------------------------------


class TestTrainEndToEnd:
    def _train_small(self, model_path: Path, *, persist_data: bool = True) -> ModelCard:
        return train(
            n_samples=300,
            model_path=model_path,
            n_estimators=30,
            persist_data=persist_data,
        )

    def test_returns_model_card(self, tmp_path: Path) -> None:
        card = self._train_small(tmp_path / "ato.ubj")
        assert isinstance(card, ModelCard)
        assert isinstance(card.training, TrainingReport)
        assert card.feature_names == list(FEATURE_NAMES)
        assert card.heuristic_label_version == HEURISTIC_LABEL_VERSION
        assert card.fast_path_allow_threshold == 0.20
        assert card.fast_path_block_threshold == 0.85

    def test_writes_artifact_and_card(self, tmp_path: Path) -> None:
        artifact = tmp_path / "ato.ubj"
        self._train_small(artifact)
        assert artifact.exists()
        card_path = artifact.with_suffix(".json")
        assert card_path.exists()
        # The card on disk re-validates as a ModelCard.
        ModelCard.model_validate_json(card_path.read_text(encoding="utf-8"))

    def test_card_sha256_matches_artifact(self, tmp_path: Path) -> None:
        artifact = tmp_path / "ato.ubj"
        card = self._train_small(artifact)
        actual = hashlib.sha256(artifact.read_bytes()).hexdigest()
        assert card.artifact_sha256 == actual

    def test_meets_test_auc_floor(self, tmp_path: Path) -> None:
        # Heuristic labels should be highly recoverable on a small synthetic
        # set — 0.9 floor is conservative for 300 samples and 30 trees.
        card = self._train_small(tmp_path / "ato.ubj")
        assert card.training.test_auc > 0.9, card.training

    def test_routing_distribution_sums_to_one(self, tmp_path: Path) -> None:
        card = self._train_small(tmp_path / "ato.ubj")
        total = sum(card.training.test_routing_distribution.values())
        assert total > 0.999
        assert total < 1.001

    def test_persist_data_writes_csvs(self, tmp_path: Path) -> None:
        artifact = tmp_path / "ato.ubj"
        card = self._train_small(artifact, persist_data=True)
        train_csv = artifact.parent / f"{card.model_id}.train.csv"
        test_csv = artifact.parent / f"{card.model_id}.test.csv"
        assert train_csv.exists()
        assert test_csv.exists()
        # CSVs include the label column.
        with train_csv.open(encoding="utf-8") as f:
            header = f.readline().strip().split(",")
        assert header == [*FEATURE_NAMES, "label"]

    def test_no_persist_skips_csvs(self, tmp_path: Path) -> None:
        artifact = tmp_path / "ato.ubj"
        card = self._train_small(artifact, persist_data=False)
        train_csv = artifact.parent / f"{card.model_id}.train.csv"
        test_csv = artifact.parent / f"{card.model_id}.test.csv"
        assert not train_csv.exists()
        assert not test_csv.exists()
