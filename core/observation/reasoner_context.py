"""ReasonerContext — generic lattice for domain reasoner evidence.

ReasonerContext is the framework's domain-agnostic contract for what any
domain reasoner must provide when submitting an Observation. It decouples
the framework from upstream model internals: the framework stores the
context verbatim in the DecisionBundle and uses only ``reasoner_id``,
``model_version``, and ``inference_latency_ms`` for telemetry. Label
semantics and feature interpretation are the domain's concern.

Domain assemblers translate internal model output types into ReasonerContext
before populating the outbound Observation. This translation step is the
handoff boundary between domain and framework.

Extension point — OperatingMode:
    A future ``OperatingMode`` enum (GOVERNED | AUDIT_ONLY) will be added here
    when multi-tenant registration is implemented. AUDIT_ONLY mode allows
    domain reasoners that embed policy rules upstream (or have no gate
    requirement) to use DecisionLedger as a pure audit record system without
    providing GateContext. In the reference MVP, all observations are GOVERNED.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Contribution(BaseModel):
    """Per-event feature contribution from a domain reasoner.

    Carried in ``AttributionSummary.feature_contributions`` to provide
    per-observation feature attribution in the DecisionBundle.

    Works with any feature attribution method: SHAP values, LIME
    coefficients, rule weights, attention scores, etc. Sign and magnitude
    semantics are defined by ``method`` — the framework carries the value
    without interpreting it. For example, SHAP values are signed (positive
    pushes the prediction toward the positive class; negative pushes away),
    while attention scores are typically non-negative.

    Attributes:
        feature_name: Name of the contributing feature (snake_case).
        feature_value: Raw feature value as seen by the model at inference
            time.
        method: Attribution method that produced the contribution value
            (e.g., ``"shap"``, ``"lime"``, ``"rule_weight"``,
            ``"attention"``). Set by the domain assembler.
            Descriptive — the framework never switches on this field.
        value: Numeric contribution value from the attribution method.
            Sign and magnitude semantics vary by method — see ``method``.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    feature_name: str
    feature_value: float
    method: str
    value: float


class LabelType(StrEnum):
    """Classification of the domain model's output type.

    Attributes:
        NUMERICAL: Continuous score output (e.g., probability ∈ [0.0, 1.0]).
            Typical of regression models and probability estimators.
        CATEGORICAL: Discrete class label output (e.g., ``"POSITIVE"``,
            ``"NEGATIVE"``). Typical of classifiers with a hard decision
            boundary.
    """

    NUMERICAL = "NUMERICAL"
    CATEGORICAL = "CATEGORICAL"


class AttributionSummary(BaseModel):
    """Flexible attribution evidence from a domain reasoner.

    Accommodates three forms of model attribution — all optional — to support
    the range of what different model types can produce:

    - ``feature_contributions``: per-event contributions from any attribution
      method (why THIS prediction for THIS event).
    - ``feature_importance``: global model-level weights from training
      (which features matter most in general — not per-event).
    - ``narrative``: free-form explanation for cases where neither structured
      form applies (e.g., vendor black-box models, rule-based reasoners).

    At least one field should be populated for meaningful audit quality.
    The framework stores this verbatim in the DecisionBundle without
    interpreting its contents.

    Attributes:
        feature_contributions: Per-event feature contributions ranked by
            magnitude. Present when the model produces per-observation
            attribution (SHAP, LIME, rule weights, etc.). None when
            attribution is not available.
        feature_importance: Global feature weight map from model training
            (feature name → importance score). Provides model-level
            explainability when per-event attribution is unavailable.
        narrative: Free-form explanation. Used when neither structured form
            applies, or to supplement them with domain context.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    feature_contributions: list[Contribution] | None = None
    feature_importance: dict[str, float] | None = None
    narrative: str | None = None


class ReasonerContext(BaseModel):
    """Generic lattice for domain reasoner evidence submitted to DecisionLedger.

    All fields are domain-agnostic. Domain assemblers translate their internal
    model output types into this contract before submitting an Observation to
    the framework. The framework stores ReasonerContext verbatim in the
    DecisionBundle.

    ``label_type``, ``label_name``, and ``label_value`` together describe the
    model's prediction in a self-documenting way that is interpretable without
    domain knowledge — for example, a bundle reader can understand
    ``label_type=NUMERICAL, label_name="risk_score", label_value=0.87``
    without knowing anything about the originating domain.

    ``feature_set`` captures the complete feature snapshot at the time of
    prediction. This is what makes decisions replayable and auditable from
    the DecisionBundle without calling back to the source reasoner.

    Attributes:
        reasoner_id: Machine identifier for this reasoner, matching the
            DecisionLedger registration record. Used as the stable key for
            cross-bundle queries and comparisons.
        reasoner_name: Human-readable display name. Recorded in the bundle
            for display contexts — dashboards, review packets, audit
            reports.
        model_version: Model artifact version string. Enables detection of
            version drift between the original decision and any
            retrospective replay.
        model_artifact_sha256: SHA-256 hex digest of the model binary at
            load time. Provides content-addressable proof of which exact
            artifact produced the prediction — compare against a candidate
            file to verify. ``None`` for API-based or vendor models where
            no local artifact exists. Artifact storage and discovery are
            the domain's responsibility (or an external model registry's).
        inference_latency_ms: Wall-clock inference time in milliseconds.
            Recorded in the bundle latency breakdown. ``None`` for batch
            reasoners where per-observation latency is not meaningful.
        label_type: Whether the model output is a continuous score or a
            discrete class label.
        label_name: Semantic name of the output label
            (e.g., ``"risk_score"``, ``"fraud_probability"``,
            ``"churn_label"``). Self-documents the prediction in the bundle.
        label_value: The model's prediction. ``float`` for ``NUMERICAL``;
            ``str`` for ``CATEGORICAL``.
        feature_set: Complete snapshot of the named feature values the model
            consumed at prediction time. Enables audit and replay without
            calling back to the source reasoner. Keys are feature names
            (snake_case); values are the raw feature values.
        attribution: Optional attribution evidence. Strongly encouraged for
            audit quality — omitting it reduces the explainability of
            decisions in the bundle.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    # Reasoner identity
    reasoner_id: str
    reasoner_name: str
    model_version: str
    model_artifact_sha256: str | None = None
    inference_latency_ms: float | None = Field(default=None, ge=0.0)

    # Prediction
    label_type: LabelType
    label_name: str
    label_value: float | str

    # Evidence
    feature_set: dict[str, float | int | str | bool]
    attribution: AttributionSummary | None = None
