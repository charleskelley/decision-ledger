"""Heuristic label function and training pipeline for the ATO XGBoost scorer.

The XGBoost model approximates a heuristic labeling function — it is a risk
triage classifier, not a fraud detector. It provides TreeSHAP attribution for
per-event explainability and routes events into confidence bands.

The ``train`` function is the single entry point. It generates a synthetic
dataset, splits it 80/20 into train/test, fits XGBoost, computes evaluation
metrics on both partitions, persists the train/test data as CSVs alongside
the binary, and writes a ``ModelCard`` sidecar JSON capturing reproducibility
context, performance, and routing sanity.

Usage::

    from pathlib import Path
    from app.scorer.trainer import train

    card = train(model_path=Path("app/scorer/models/ato-v1.ubj"))
    print(card.training.test_auc)
"""

from __future__ import annotations

import hashlib
import random
import subprocess
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003

import numpy as np
import pandas as pd
import xgboost as xgb

from app.scorer.eval import (
    TrainingReport,
    confusion_matrix_at_threshold,
    log_loss,
    precision_recall_at_threshold,
    roc_auc,
    routing_distribution,
)
from app.scorer.model_card import ModelCard
from app.scorer.scorer import (
    FAST_PATH_ALLOW_THRESHOLD,
    FAST_PATH_BLOCK_THRESHOLD,
    FEATURE_NAMES,
)

_SEED: int = 42

HEURISTIC_LABEL_VERSION: str = "1.0"
"""Version tag for the heuristic labeling function. Bump on rule changes."""

DEFAULT_MODEL_ID: str = "ato-v1"
DEFAULT_MODEL_VERSION: str = "1.0.0"


def _heuristic_label(row: dict[str, float]) -> float:
    """Compute heuristic risk score for a synthetic sample in [0, 1].

    Combines additive risk signals derived from velocity, novelty, travel,
    and consistency features into a continuous score. The binary label used
    for training is obtained by thresholding this score at 0.5.

    Args:
        row: A dict mapping FEATURE_NAMES keys to float values.

    Returns:
        A float in [0.0, 1.0] representing heuristic risk.
    """
    score = 0.0

    v1 = row["velocity_1min"]
    if v1 > 5:
        score += 0.35
    elif v1 > 2:
        score += 0.15
    elif v1 > 1:
        score += 0.05

    v5 = row["velocity_5min"]
    if v5 > 15:
        score += 0.20
    elif v5 > 8:
        score += 0.10

    if row["impossible_travel"] > 0.5:
        score += 0.45

    if row["ip_novelty"] > 0.5:
        score += 0.15

    if row["device_novelty"] > 0.5:
        score += 0.20

    if row["geo_novelty"] > 0.5:
        score += 0.25

    if row["sparse_history"] > 0.5:
        score += 0.15

    score += (1.0 - row["device_consistency_score"]) * 0.10
    score += (1.0 - row["user_agent_consistency"]) * 0.08

    return min(1.0, score)


def _generate_sample(rng: random.Random) -> dict[str, float]:
    """Generate one synthetic ATO feature sample using the provided RNG.

    Samples from five archetype distributions based on a uniform draw:

    - ``[0.00, 0.55)``: normal / low-risk login
    - ``[0.55, 0.70)``: high-velocity credential-stuffing pattern
    - ``[0.70, 0.80)``: impossible-travel pattern
    - ``[0.80, 0.90)``: novel entity with sparse history
    - ``[0.90, 1.00]``: mixed / ambiguous

    Args:
        rng: A seeded :class:`random.Random` instance for reproducibility.

    Returns:
        A dict with all keys in FEATURE_NAMES mapped to float values.
    """
    roll = rng.random()

    if roll < 0.55:
        # Normal / low-risk
        return {
            "velocity_1min": float(rng.randint(0, 1)),
            "velocity_5min": float(rng.randint(0, 3)),
            "velocity_60min": float(rng.randint(1, 8)),
            "velocity_1440min": float(rng.randint(5, 30)),
            "ip_novelty": 0.0,
            "device_novelty": 0.0,
            "geo_novelty": 0.0,
            "impossible_travel": 0.0,
            "travel_speed_kmh": 0.0,
            "device_consistency_score": rng.uniform(0.80, 0.98),
            "user_agent_consistency": rng.uniform(0.85, 0.99),
            "sparse_history": 0.0,
        }

    if roll < 0.70:
        # High-velocity / credential stuffing
        return {
            "velocity_1min": float(rng.randint(5, 15)),
            "velocity_5min": float(rng.randint(12, 40)),
            "velocity_60min": float(rng.randint(30, 100)),
            "velocity_1440min": float(rng.randint(50, 200)),
            "ip_novelty": 1.0,
            "device_novelty": float(rng.choice([0.0, 1.0])),
            "geo_novelty": float(rng.choice([0.0, 1.0])),
            "impossible_travel": 0.0,
            "travel_speed_kmh": 0.0,
            "device_consistency_score": rng.uniform(0.05, 0.30),
            "user_agent_consistency": rng.uniform(0.05, 0.30),
            "sparse_history": 0.0,
        }

    if roll < 0.80:
        # Impossible travel
        return {
            "velocity_1min": float(rng.randint(0, 2)),
            "velocity_5min": float(rng.randint(0, 5)),
            "velocity_60min": float(rng.randint(1, 10)),
            "velocity_1440min": float(rng.randint(5, 25)),
            "ip_novelty": 1.0,
            "device_novelty": 0.0,
            "geo_novelty": 1.0,
            "impossible_travel": 1.0,
            "travel_speed_kmh": float(rng.randint(1000, 5000)),
            "device_consistency_score": rng.uniform(0.60, 0.95),
            "user_agent_consistency": rng.uniform(0.70, 0.99),
            "sparse_history": 0.0,
        }

    if roll < 0.90:
        # Novel entity / sparse history
        return {
            "velocity_1min": float(rng.randint(0, 1)),
            "velocity_5min": float(rng.randint(0, 2)),
            "velocity_60min": float(rng.randint(0, 5)),
            "velocity_1440min": float(rng.randint(0, 4)),
            "ip_novelty": 0.5,
            "device_novelty": 0.5,
            "geo_novelty": 0.5,
            "impossible_travel": 0.0,
            "travel_speed_kmh": 0.0,
            "device_consistency_score": 0.5,
            "user_agent_consistency": 0.5,
            "sparse_history": 1.0,
        }

    # Mixed / ambiguous
    return {
        "velocity_1min": float(rng.randint(0, 3)),
        "velocity_5min": float(rng.randint(0, 8)),
        "velocity_60min": float(rng.randint(1, 15)),
        "velocity_1440min": float(rng.randint(3, 20)),
        "ip_novelty": float(rng.choice([0.0, 1.0])),
        "device_novelty": float(rng.choice([0.0, 1.0])),
        "geo_novelty": float(rng.choice([0.0, 1.0])),
        "impossible_travel": 0.0,
        "travel_speed_kmh": 0.0,
        "device_consistency_score": rng.uniform(0.30, 0.90),
        "user_agent_consistency": rng.uniform(0.30, 0.90),
        "sparse_history": float(rng.choice([0.0, 1.0])),
    }


def _generate_dataset(n: int) -> tuple[np.ndarray, np.ndarray]:
    """Generate a synthetic labeled dataset of ATO feature vectors.

    Uses a fixed seed for reproducibility. Labels are produced by applying
    ``_heuristic_label`` and binarizing at 0.5 (> 0.5 → 1.0, else 0.0).

    Args:
        n: Number of samples to generate.

    Returns:
        A tuple ``(x_arr, y)`` where ``x_arr`` has shape
        ``(n, len(FEATURE_NAMES))`` and dtype float32, and ``y`` has shape
        ``(n,)`` and dtype float32.
    """
    rng = random.Random(_SEED)  # noqa: S311
    rows: list[list[float]] = []
    labels: list[float] = []

    for _ in range(n):
        sample = _generate_sample(rng)
        raw_label = _heuristic_label(sample)
        rows.append([sample[f] for f in FEATURE_NAMES])
        labels.append(1.0 if raw_label > 0.5 else 0.0)

    x_arr = np.array(rows, dtype=np.float32)
    y = np.array(labels, dtype=np.float32)
    return x_arr, y


def _train_test_split(
    x: np.ndarray,
    y: np.ndarray,
    *,
    test_fraction: float = 0.2,
    seed: int = _SEED,
) -> tuple[tuple[np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray]]:
    """Deterministic train/test split via a seeded numpy permutation.

    Args:
        x: Feature matrix shape ``(n, n_features)``.
        y: Labels shape ``(n,)``.
        test_fraction: Fraction in ``[0, 1]`` to hold out as the test set.
        seed: Seed for the permutation. Same seed → identical split.

    Returns:
        ``((x_train, y_train), (x_test, y_test))`` with disjoint indices.
    """
    rng = np.random.default_rng(seed)
    indices = rng.permutation(len(y))
    n_test = int(len(y) * test_fraction)
    test_idx, train_idx = indices[:n_test], indices[n_test:]
    return (x[train_idx], y[train_idx]), (x[test_idx], y[test_idx])


def _persist_dataset(x: np.ndarray, y: np.ndarray, *, path: Path) -> None:
    """Write a feature matrix + label column to a CSV file.

    Columns are ``FEATURE_NAMES`` followed by ``label``.
    """
    df = pd.DataFrame(x, columns=FEATURE_NAMES)
    df["label"] = y
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def _capture_git_sha() -> str | None:
    """Return the current git HEAD SHA, or ``None`` if unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 (git intentionally on PATH)
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _build_training_report(
    *,
    model: xgb.Booster,
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_test: np.ndarray,
    y_test: np.ndarray,
) -> TrainingReport:
    """Run inference on both partitions and assemble a TrainingReport."""
    train_dmat = xgb.DMatrix(x_train, label=y_train, feature_names=FEATURE_NAMES)
    test_dmat = xgb.DMatrix(x_test, label=y_test, feature_names=FEATURE_NAMES)
    train_pred = np.clip(model.predict(train_dmat), 0.0, 1.0)
    test_pred = np.clip(model.predict(test_dmat), 0.0, 1.0)

    test_precision, test_recall = precision_recall_at_threshold(y_test, test_pred, 0.5)
    test_cm = confusion_matrix_at_threshold(y_test, test_pred, 0.5)
    test_routing = routing_distribution(
        test_pred,
        allow_threshold=FAST_PATH_ALLOW_THRESHOLD,
        block_threshold=FAST_PATH_BLOCK_THRESHOLD,
    )

    return TrainingReport(
        n_train=len(y_train),
        n_test=len(y_test),
        train_auc=roc_auc(y_train, train_pred),
        test_auc=roc_auc(y_test, test_pred),
        train_log_loss=log_loss(y_train, train_pred),
        test_log_loss=log_loss(y_test, test_pred),
        test_precision_at_0_5=test_precision,
        test_recall_at_0_5=test_recall,
        test_confusion_matrix=test_cm,
        test_routing_distribution=test_routing,
    )


def train(
    n_samples: int = 5000,
    *,
    model_path: Path,
    n_estimators: int = 100,
    max_depth: int = 4,
    learning_rate: float = 0.1,
    persist_data: bool = True,
    model_id: str = DEFAULT_MODEL_ID,
    model_version: str = DEFAULT_MODEL_VERSION,
) -> ModelCard:
    """Train the ATO XGBoost scorer end-to-end.

    Generates synthetic data, splits 80/20, fits XGBoost on the train
    partition, evaluates on both partitions, and writes the model artifact
    plus a sidecar ``ModelCard`` JSON. Optionally persists the train and
    test feature matrices as CSVs alongside the artifact for reproducibility
    and inspection.

    Args:
        n_samples: Number of synthetic training samples to generate.
        model_path: Destination ``.ubj`` path. The card sidecar is written
            to ``model_path.with_suffix(".json")``; train/test CSVs to
            ``<model_id>.train.csv`` and ``<model_id>.test.csv`` in the same
            directory.
        n_estimators: Number of boosting rounds.
        max_depth: Maximum tree depth per estimator.
        learning_rate: Step-size shrinkage (XGBoost ``eta``).
        persist_data: When ``True`` (default), write train/test CSVs next to
            the artifact. Tests can pass ``False`` to keep tmp dirs clean.
        model_id: Stable identifier recorded in the card.
        model_version: Semantic version recorded in the card.

    Returns:
        The persisted ``ModelCard`` (also saved as JSON next to the binary).
    """
    x_arr, y = _generate_dataset(n_samples)
    (x_train, y_train), (x_test, y_test) = _train_test_split(x_arr, y)

    train_dmat = xgb.DMatrix(x_train, label=y_train, feature_names=FEATURE_NAMES)
    params = {
        "objective": "binary:logistic",
        "eval_metric": "auc",
        "max_depth": max_depth,
        "eta": learning_rate,
        "seed": _SEED,
        "tree_method": "hist",
    }
    model = xgb.train(params, train_dmat, num_boost_round=n_estimators)

    model_path.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(model_path))
    artifact_sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest()

    report = _build_training_report(
        model=model,
        x_train=x_train,
        y_train=y_train,
        x_test=x_test,
        y_test=y_test,
    )

    if persist_data:
        models_dir = model_path.parent
        _persist_dataset(x_train, y_train, path=models_dir / f"{model_id}.train.csv")
        _persist_dataset(x_test, y_test, path=models_dir / f"{model_id}.test.csv")

    card = ModelCard(
        model_id=model_id,
        model_version=model_version,
        created_at=datetime.now(UTC),
        git_sha=_capture_git_sha(),
        seed=_SEED,
        n_samples=n_samples,
        n_estimators=n_estimators,
        max_depth=max_depth,
        learning_rate=learning_rate,
        feature_names=list(FEATURE_NAMES),
        heuristic_label_version=HEURISTIC_LABEL_VERSION,
        training=report,
        fast_path_allow_threshold=FAST_PATH_ALLOW_THRESHOLD,
        fast_path_block_threshold=FAST_PATH_BLOCK_THRESHOLD,
        artifact_sha256=artifact_sha256,
    )
    card_path = model_path.with_suffix(".json")
    card_path.write_text(card.model_dump_json(indent=2), encoding="utf-8")

    return card
