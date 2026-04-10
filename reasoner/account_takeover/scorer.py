"""ATO Reasoner scorer output — internal domain type.

ScorerOutput is the ATO Reasoner's internal representation of the fast ML
scorer's result. It is produced by app/scorer/ and consumed by the domain
layer when assembling the Observation (specifically FastPathRecord and
GateContext.template_vars). It is not a framework contract and does not
appear in DecisionBundle or any core/decision/ type.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from core.gate import GateRouting
from core.observation import Signal


class ScorerOutput(BaseModel):
    """ATO fast ML scorer output including risk score and SHAP signals.

    Produced by the XGBoost scorer in app/scorer/. The domain layer
    consumes this to populate FastPathRecord and GateContext.template_vars
    on the outbound Observation before submitting to the DecisionLedger
    framework.

    Args:
        entity_id: Framework UUID of the subject the score was computed for.
        risk_score: Risk probability estimate in [0.0, 1.0].
        top_signals: Top-k signals ranked by absolute SHAP value.
        scorer_version: Loaded model artifact version (e.g., ``"xgb-v1.2.0"``).
        inference_latency_ms: Wall-clock inference time in milliseconds.
        routing: Routing decision produced by the confidence-band threshold.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    entity_id: UUID
    risk_score: float = Field(ge=0.0, le=1.0)
    top_signals: list[Signal]
    scorer_version: str
    inference_latency_ms: float = Field(ge=0.0)
    routing: GateRouting
