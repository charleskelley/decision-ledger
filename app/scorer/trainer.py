"""Heuristic label function and training pipeline for the ATO XGBoost scorer.

The XGBoost model approximates a heuristic labeling function — it is a risk
triage classifier, not a fraud detector. It provides TreeSHAP attribution for
per-event explainability and routes events into confidence bands.

Usage::

    from pathlib import Path
    from app.scorer.trainer import train

    train(n_samples=2000, model_path=Path("app/scorer/models/ato-v1.ubj"))
"""

from __future__ import annotations

import random
from pathlib import Path  # noqa: TC003

import numpy as np
import xgboost as xgb

from app.scorer.scorer import FEATURE_NAMES

_SEED: int = 42


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


def train(
    n_samples: int = 2000,
    *,
    model_path: Path,
    n_estimators: int = 100,
    max_depth: int = 4,
    learning_rate: float = 0.1,
) -> None:
    """Train the ATO XGBoost scorer and save the model artifact.

    Generates a synthetic labeled dataset using the heuristic labeling
    function, trains a binary:logistic XGBoost model, and saves the
    resulting artifact at ``model_path``.

    Args:
        n_samples: Number of synthetic training samples to generate.
        model_path: Destination path for the saved model artifact.
        n_estimators: Number of boosting rounds.
        max_depth: Maximum tree depth per estimator.
        learning_rate: Step size shrinkage (``eta`` in XGBoost params).
    """
    x_arr, y = _generate_dataset(n_samples)
    dtrain = xgb.DMatrix(x_arr, label=y, feature_names=FEATURE_NAMES)

    params = {
        "objective": "binary:logistic",
        "eval_metric": "auc",
        "max_depth": max_depth,
        "eta": learning_rate,
        "seed": _SEED,
        "tree_method": "hist",
    }

    model = xgb.train(params, dtrain, num_boost_round=n_estimators)

    model_path.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(model_path))
