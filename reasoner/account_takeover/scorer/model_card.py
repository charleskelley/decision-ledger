"""ModelCard — sidecar metadata for a trained ATO scorer artifact.

Persisted as JSON next to the XGBoost binary (``ato-v1.ubj`` ⇄
``ato-v1.json``). The trainer writes the card; ``AtoScorer`` reads and
validates it on load. The card captures everything needed to:

  - Verify the binary hasn't been tampered with (``artifact_sha256``).
  - Verify the feature schema matches what the runtime expects
    (``feature_names``).
  - Reproduce the training run (``seed``, hyperparameters, sample size).
  - Audit-trail the model's behavior (``TrainingReport`` block,
    routing-distribution sanity, configured fast-path thresholds).

Living inside the ATO scorer package keeps the framework's ``core/`` boundary
clean of model-management types — ``ScorerOutput`` (the framework-side contract)
remains unchanged.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from reasoner.account_takeover.scorer.eval import TrainingReport


class ModelCard(BaseModel):
    """Sidecar metadata for a trained ATO scorer artifact.

    The card is written as JSON alongside the XGBoost binary at training
    time and validated by ``AtoScorer.__init__`` at load time. Mismatches
    between the card and the binary (feature-schema drift, SHA-256 tamper)
    raise at load time rather than producing silent garbage scores.

    Attributes:
        model_id: Stable identifier (e.g., ``"ato-v1"``). Matches the
            artifact filename stem.
        model_version: Semantic version of the model artifact.
        created_at: UTC timestamp when training completed.
        git_sha: Repository commit SHA at training time. ``None`` when the
            trainer ran outside a git checkout (e.g., CI without history).
        seed: Random seed used for sample generation, split, and XGBoost
            training. Same seed → byte-identical artifact.
        n_samples: Total samples generated before the train/test split.
        n_estimators: Number of boosting rounds.
        max_depth: Maximum tree depth.
        learning_rate: XGBoost ``eta`` parameter.
        feature_names: Ordered list of input feature names. Must match
            the scorer's ``FEATURE_NAMES`` at load time — mismatch is a
            feature-schema drift error.
        heuristic_label_version: Version tag for the heuristic labeling
            function in the ATO scorer trainer. Bump on label changes so
            historical cards remain interpretable.
        training: Training and evaluation metrics from the run.
        fast_path_allow_threshold: Lower routing-band cutoff at training
            time. Recorded so the routing distribution in
            ``training.test_routing_distribution`` is interpretable later
            even if production thresholds drift.
        fast_path_block_threshold: Upper routing-band cutoff at training
            time.
        artifact_sha256: SHA-256 of the XGBoost binary as written. Used to
            detect post-write tampering at load time.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    # Identity
    model_id: str
    model_version: str
    created_at: datetime
    git_sha: str | None

    # Reproducibility
    seed: int
    n_samples: int = Field(ge=0)
    n_estimators: int = Field(ge=1)
    max_depth: int = Field(ge=1)
    learning_rate: float = Field(gt=0.0)
    feature_names: list[str]
    heuristic_label_version: str

    # Performance + routing
    training: TrainingReport
    fast_path_allow_threshold: float = Field(ge=0.0, le=1.0)
    fast_path_block_threshold: float = Field(ge=0.0, le=1.0)

    # Integrity
    artifact_sha256: str


__all__ = ["ModelCard"]
