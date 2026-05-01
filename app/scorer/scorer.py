"""ATO XGBoost fast scorer — risk score and TreeSHAP attribution.

AtoScorer wraps a trained XGBoost binary:logistic model and produces a
ScorerOutput including the risk probability, top-k SHAP signals, and
routing decision. Target inference latency: < 10 ms P95.

SHAP values are computed via XGBoost's native TreeSHAP (pred_contribs=True),
which returns shape (n_samples, n_features + 1); the last column is the bias
term and is excluded from the reported signals.

Card-aware loading
------------------

When a ``ModelCard`` JSON sidecar exists next to the artifact (same path,
``.json`` extension), ``AtoScorer.__init__`` validates two integrity
properties before serving inference:

  - ``feature_names`` in the card matches ``FEATURE_NAMES`` here. Mismatch
    indicates training/runtime feature-schema drift — raises ``ValueError``
    rather than producing silent garbage scores.
  - ``artifact_sha256`` in the card matches the hash of the binary on disk.
    Mismatch indicates the binary was modified after the card was written —
    raises ``ValueError``.

Legacy artifacts without a sidecar still load (with a warning), preserving
backward compatibility for cached test fixtures and historical bundles.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import TYPE_CHECKING

import numpy as np
import xgboost as xgb

from app.scorer.model_card import ModelCard
from core.observation import Contribution
from core.routes import GateRoute
from reasoner.account_takeover.scorer import ScorerOutput

if TYPE_CHECKING:
    from pathlib import Path

    from reasoner.account_takeover.features import AtoFeatureVector

_log = logging.getLogger(__name__)

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

FAST_PATH_ALLOW_THRESHOLD: float = 0.20
"""Risk-score threshold below which an event routes FAST_PATH_ALLOW."""

FAST_PATH_BLOCK_THRESHOLD: float = 0.85
"""Risk-score threshold above which an event routes FAST_PATH_BLOCK."""

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


def _route(risk_score: float) -> GateRoute:
    """Map a risk score to a GateRoute using confidence-band thresholds.

    Args:
        risk_score: Risk probability in [0.0, 1.0].

    Returns:
        FAST_PATH_ALLOW if below the allow threshold,
        FAST_PATH_BLOCK if above the block threshold,
        ROUTE_TO_GATE otherwise.
    """
    if risk_score < FAST_PATH_ALLOW_THRESHOLD:
        return GateRoute.FAST_PATH_ALLOW
    if risk_score > FAST_PATH_BLOCK_THRESHOLD:
        return GateRoute.FAST_PATH_BLOCK
    return GateRoute.ROUTE_TO_GATE


def _top_signals(
    shap_values: np.ndarray,
    feature_row: np.ndarray,
    k: int,
) -> list[Contribution]:
    """Select top-k features by absolute SHAP contribution.

    Args:
        shap_values: 1-D array of SHAP values, one per feature (bias excluded).
        feature_row: 1-D array of raw feature values in FEATURE_NAMES order.
        k: Number of top signals to return.

    Returns:
        Up to ``k`` Contribution objects ranked by descending absolute SHAP value.
    """
    indices = np.argsort(np.abs(shap_values))[::-1][:k]
    return [
        Contribution(
            feature_name=FEATURE_NAMES[i],
            feature_value=float(feature_row[i]),
            method="shap",
            value=float(shap_values[i]),
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
            Defaults to ``ModelCard.model_version`` when a sidecar card is
            present, else the model file stem (e.g., ``"ato-v1"``).
    """

    def __init__(
        self,
        model_path: Path,
        *,
        scorer_version: str | None = None,
    ) -> None:
        """Load the XGBoost model artifact and validate the sidecar card.

        Args:
            model_path: Path to the XGBoost model file. A ``ModelCard``
                sidecar at ``model_path.with_suffix(".json")`` is loaded
                and validated when present.
            scorer_version: Override for the version string recorded in
                ``ScorerOutput``. When ``None`` (default), uses the card's
                ``model_version`` if a card exists, else the file stem.

        Raises:
            ValueError: If a sidecar card exists and either (a) its
                ``feature_names`` doesn't match ``FEATURE_NAMES`` (schema
                drift) or (b) its ``artifact_sha256`` doesn't match the
                binary on disk (post-write tampering).
        """
        self._model = xgb.Booster()
        self._model.load_model(str(model_path))
        self._artifact_sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest()

        card = self._load_card_if_present(model_path)
        if card is not None:
            self._validate_card(card)
        self._card = card

        if scorer_version is not None:
            self._version = scorer_version
        elif card is not None:
            self._version = card.model_version
        else:
            self._version = model_path.stem

    @property
    def model_card(self) -> ModelCard | None:
        """The validated ``ModelCard`` sidecar, or ``None`` for legacy artifacts."""
        return self._card

    @staticmethod
    def _load_card_if_present(model_path: Path) -> ModelCard | None:
        """Return the parsed sidecar card if it exists, else None with a warning."""
        card_path = model_path.with_suffix(".json")
        if not card_path.exists():
            _log.warning(
                "scorer.no_model_card",
                extra={
                    "component": "scorer",
                    "model_path": str(model_path),
                    "expected_card_path": str(card_path),
                },
            )
            return None
        return ModelCard.model_validate_json(card_path.read_text(encoding="utf-8"))

    def _validate_card(self, card: ModelCard) -> None:
        """Raise ``ValueError`` on feature-schema or artifact-integrity mismatch."""
        if card.feature_names != FEATURE_NAMES:
            raise ValueError(
                "ModelCard feature_names mismatch with runtime FEATURE_NAMES. "
                f"card={card.feature_names!r} runtime={FEATURE_NAMES!r}"
            )
        if card.artifact_sha256 != self._artifact_sha256:
            raise ValueError(
                "ModelCard artifact_sha256 mismatch with binary on disk. "
                f"card={card.artifact_sha256!r} actual={self._artifact_sha256!r}"
            )

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
            scorer_artifact_sha256=self._artifact_sha256,
            inference_latency_ms=latency_ms,
            route=_route(risk_score),
        )
