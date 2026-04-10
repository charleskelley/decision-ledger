"""ReasonerContext — generic lattice for domain reasoner evidence.

ReasonerContext is the framework's domain-agnostic contract for what any
domain reasoner must provide when submitting an Observation. It decouples
the framework from scorer internals: the framework stores the context verbatim
in the DecisionBundle and uses only ``reasoner_id``, ``model_version``, and
``inference_latency_ms`` for telemetry. Label semantics and feature
interpretation are the domain's concern.

Domain assemblers translate internal scorer output types (e.g., ScorerOutput
in reasoner/account_takeover/) into ReasonerContext before populating the
outbound Observation. This translation step is the handoff boundary.

Extension point — OperatingMode:
    A future ``OperatingMode`` enum (GOVERNED | AUDIT_ONLY) will be added here
    when multi-tenant registration is implemented. AUDIT_ONLY mode allows
    domain reasoners that embed policy rules upstream (or have no policy gate
    requirement) to use DecisionLedger as a pure audit record system without
    providing GateContext. In the reference MVP, all observations are GOVERNED.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Signal(BaseModel):
    """Individual SHAP-attributed feature signal from a domain reasoner.

    Carried in ``AttributionSummary.observation_signals`` to provide
    per-observation feature attribution in the DecisionBundle. Negative
    ``shap_value`` is valid and meaningful — it represents a feature that
    decreases predicted risk (e.g., a known device, low velocity).

    Args:
        feature_name: Name of the contributing feature (snake_case).
        shap_value: SHAP attribution value. Signed — positive values increase
            predicted risk; negative values decrease it.
        raw_value: Raw feature value as seen by the model at inference time.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    feature_name: str
    shap_value: float
    raw_value: float


class LabelType(StrEnum):
    """Classification of the domain model's output type.

    Attributes:
        NUMERICAL: Continuous score output (e.g., risk_score ∈ [0.0, 1.0]).
            Typical of regression models and probability estimators.
        CATEGORICAL: Discrete class label output (e.g., ``"FRAUD"``,
            ``"LEGITIMATE"``). Typical of classifiers with a hard decision
            boundary.
    """

    NUMERICAL = "NUMERICAL"
    CATEGORICAL = "CATEGORICAL"


class AttributionSummary(BaseModel):
    """Flexible attribution evidence from the domain reasoner.

    Accommodates three forms of model attribution — all optional — to support
    the range of what different model types can produce:

    - ``observation_signals``: per-observation SHAP values or equivalent
      attribution (why THIS decision for THIS event).
    - ``feature_importance``: global model-level weights from training
      research (which features matter most in general — not per-event).
    - ``narrative``: free-form explanation for cases where neither structured
      form applies (e.g., vendor black-box models, rule-based reasoners).

    At least one field should be populated for meaningful audit quality.
    The framework stores this verbatim in the DecisionBundle without
    interpreting its contents.

    Args:
        observation_signals: Top-k SHAP-attributed signals ranked by absolute
            contribution. Present when the model produces per-observation
            feature attribution. None when attribution is not available at
            inference time.
        feature_importance: Global feature weight map from model training
            (feature name → importance score). Provides model-level
            explainability when per-observation attribution is unavailable.
        narrative: Free-form explanation. Used when neither structured form
            applies, or to supplement them with domain context.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    observation_signals: list[Signal] | None = None
    feature_importance: dict[str, float] | None = None
    narrative: str | None = None


class ReasonerContext(BaseModel):
    """Generic lattice for domain reasoner evidence submitted to DecisionLedger.

    All fields are domain-agnostic. Domain assemblers translate their internal
    scorer output types into this contract before submitting an Observation to
    the framework. The framework stores ReasonerContext verbatim in the
    DecisionBundle.

    ``label_type``, ``label_name``, and ``label_value`` together describe the
    model's prediction in a self-documenting way that is interpretable without
    domain knowledge — for example, a bundle reader can understand
    ``label_type=NUMERICAL, label_name="risk_score", label_value=0.87``
    without knowing anything about the ATO domain.

    ``feature_set`` captures the complete feature snapshot at inference time.
    This is what makes fast-path decisions replayable and auditable from the
    DecisionBundle without calling back to the source reasoner.

    Args:
        reasoner_id: Machine identifier for this reasoner, matching the
            DecisionLedger registration record (e.g., ``"ato-reasoner"``).
            Used as the stable key for cross-bundle queries and comparisons.
        reasoner_name: Human-readable display name
            (e.g., ``"ATO Reasoner v2"``). Recorded in the bundle for
            display contexts — dashboards, review packets, audit reports.
        model_version: Loaded model artifact version
            (e.g., ``"xgb-v1.2.0"``). Enables detection of model version
            drift between the original decision and any retrospective replay.
        inference_latency_ms: Wall-clock inference time in milliseconds.
            Recorded in the bundle latency breakdown.
        label_type: Whether the model output is a continuous score or a
            discrete class label.
        label_name: Semantic name of the output label
            (e.g., ``"risk_score"``, ``"fraud_probability"``,
            ``"churn_label"``). Self-documents the prediction in the bundle.
        label_value: The model's prediction. ``float`` for ``NUMERICAL``;
            ``str`` for ``CATEGORICAL``.
        feature_set: Complete snapshot of the named feature values the model
            consumed at inference time. Enables audit and replay without
            calling back to the source reasoner. Keys are feature names
            (snake_case); values are the raw feature values at inference.
        attribution: Optional attribution evidence. Strongly encouraged for
            audit quality — omitting it reduces the explainability of
            fast-path decisions in the bundle.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    # Reasoner identity
    reasoner_id: str
    reasoner_name: str
    model_version: str
    inference_latency_ms: float = Field(ge=0.0)

    # Prediction
    label_type: LabelType
    label_name: str
    label_value: float | str

    # Evidence
    feature_set: dict[str, float | int | str | bool]
    attribution: AttributionSummary | None = None
