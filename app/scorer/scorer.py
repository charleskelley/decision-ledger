"""ATO XGBoost fast scorer — risk score and TreeSHAP attribution.

AtoScorer wraps a trained XGBoost binary:logistic model and produces a
ScorerOutput including the risk probability, top-k SHAP signals, and
routing decision. Target inference latency: < 10 ms P95.

SHAP values are computed via XGBoost's native TreeSHAP (pred_contribs=True),
which returns shape (n_samples, n_features + 1); the last column is the bias
term and is excluded from the reported signals.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import numpy as np
import xgboost as xgb

from core.gate import GateRouting
from core.observation import Signal
from reasoner.account_takeover.scorer import ScorerOutput

if TYPE_CHECKING:
    from pathlib import Path

    from reasoner.account_takeover.features import AtoFeatureVector

FEATURE_NAMES: list[str] = [
    "velocity_1min",
    "velocity_5min",
    "velocity_60min",
    "velocity_1440min",
    "ip_novelty",
    "device_novelty",
    "geo_novelty",
    "impossible_travel",
    "travel_speed_kmh",
    "device_consistency_score",
    "user_agent_consistency",
    "sparse_history",
]

_FAST_PATH_ALLOW_THRESHOLD: float = 0.20
_FAST_PATH_BLOCK_THRESHOLD: float = 0.85
_TOP_K_SIGNALS: int = 5


def _to_feature_row(fv: AtoFeatureVector) -> np.ndarray:
    """Extract features from an AtoFeatureVector in FEATURE_NAMES order.

    Booleans are cast to float (True → 1.0, False → 0.0).
    travel_speed_kmh=None is mapped to 0.0.

    Args:
        fv: The ATO feature vector to extract values from.

    Returns:
        A float32 1-D array of shape ``(len(FEATURE_NAMES),)``.
    """
    return np.array(
        [
            float(fv.velocity_1min),
            float(fv.velocity_5min),
            float(fv.velocity_60min),
            float(fv.velocity_1440min),
            float(fv.ip_novelty),
            float(fv.device_novelty),
            float(fv.geo_novelty),
            1.0 if fv.impossible_travel else 0.0,
            float(fv.travel_speed_kmh) if fv.travel_speed_kmh is not None else 0.0,
            float(fv.device_consistency_score),
            float(fv.user_agent_consistency),
            1.0 if fv.sparse_history else 0.0,
        ],
        dtype=np.float32,
    )


def _routing(risk_score: float) -> GateRouting:
    """Map a risk score to a GateRouting decision using confidence-band thresholds.

    Args:
        risk_score: Risk probability in [0.0, 1.0].

    Returns:
        FAST_PATH_ALLOW if below the allow threshold,
        FAST_PATH_BLOCK if above the block threshold,
        ROUTE_TO_GATE otherwise.
    """
    if risk_score < _FAST_PATH_ALLOW_THRESHOLD:
        return GateRouting.FAST_PATH_ALLOW
    if risk_score > _FAST_PATH_BLOCK_THRESHOLD:
        return GateRouting.FAST_PATH_BLOCK
    return GateRouting.ROUTE_TO_GATE


def _top_signals(
    shap_values: np.ndarray,
    feature_row: np.ndarray,
    k: int,
) -> list[Signal]:
    """Select top-k features by absolute SHAP contribution.

    Args:
        shap_values: 1-D array of SHAP values, one per feature (bias excluded).
        feature_row: 1-D array of raw feature values in FEATURE_NAMES order.
        k: Number of top signals to return.

    Returns:
        Up to ``k`` Signal objects ranked by descending absolute SHAP value.
    """
    indices = np.argsort(np.abs(shap_values))[::-1][:k]
    return [
        Signal(
            feature_name=FEATURE_NAMES[i],
            shap_value=float(shap_values[i]),
            raw_value=float(feature_row[i]),
        )
        for i in indices
    ]


class AtoScorer:
    """XGBoost fast scorer for ATO risk triage.

    Wraps a trained XGBoost binary:logistic model and produces a ScorerOutput
    with a risk probability, TreeSHAP-attributed signals, and routing decision.
    The scorer is thread-safe for concurrent reads once constructed — XGBoost's
    Booster.predict is stateless given a fixed model.

    Args:
        model_path: Path to the XGBoost model artifact (.ubj or .json).
        scorer_version: Optional version string to embed in ScorerOutput.
            Defaults to the model file stem (e.g., ``"ato-v1"``).
    """

    def __init__(
        self,
        model_path: Path,
        *,
        scorer_version: str | None = None,
    ) -> None:
        """Load the XGBoost model artifact.

        Args:
            model_path: Path to the XGBoost model file.
            scorer_version: Version label for the loaded model. Defaults to the
                file stem when not provided.
        """
        self._model = xgb.Booster()
        self._model.load_model(str(model_path))
        self._version = scorer_version or model_path.stem

    def score(self, features: AtoFeatureVector) -> ScorerOutput:
        """Score a single ATO feature vector and return a ScorerOutput.

        Runs XGBoost inference and TreeSHAP attribution in a single forward
        pass (two predict calls share the same DMatrix). Risk score is clamped
        to [0.0, 1.0] to guard against floating-point edge cases.

        Args:
            features: The ATO feature vector to score.

        Returns:
            A ScorerOutput containing the risk score, top SHAP signals, routing
            decision, and wall-clock inference latency.
        """
        t0 = time.perf_counter()

        row = _to_feature_row(features)
        dmatrix = xgb.DMatrix(
            row.reshape(1, -1),
            feature_names=FEATURE_NAMES,
        )

        risk_score = float(self._model.predict(dmatrix)[0])
        risk_score = max(0.0, min(1.0, risk_score))

        contribs = self._model.predict(dmatrix, pred_contribs=True)
        shap_vals = contribs[0][:-1]  # exclude bias term

        latency_ms = round((time.perf_counter() - t0) * 1000, 2)

        return ScorerOutput(
            entity_id=features.entity_id,
            risk_score=risk_score,
            top_signals=_top_signals(shap_vals, row, _TOP_K_SIGNALS),
            scorer_version=self._version,
            inference_latency_ms=latency_ms,
            routing=_routing(risk_score),
        )
