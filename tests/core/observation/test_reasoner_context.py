"""Behavioral tests for ReasonerContext, LabelType, and AttributionSummary.

Key contracts:
- ReasonerContext is immutable once constructed.
- label_value accepts float (NUMERICAL) or str (CATEGORICAL).
- feature_set accepts any mix of float, int, str, bool values.
- attribution is optional — None is valid and represents unavailable attribution.
- AttributionSummary accepts any combination of its three optional fields,
  including all-None (valid but produces a low-quality audit record).
- inference_latency_ms is non-negative.
"""

import pytest
from pydantic import ValidationError

from core.observation import AttributionSummary, LabelType, ReasonerContext, Signal


def _make_reasoner_context(**overrides) -> ReasonerContext:
    """Construct a minimal valid ReasonerContext with optional field overrides."""
    defaults = {
        "reasoner_id": "test-reasoner",
        "reasoner_name": "Test Reasoner",
        "model_version": "v1.0.0",
        "inference_latency_ms": 5.0,
        "label_type": LabelType.NUMERICAL,
        "label_name": "risk_score",
        "label_value": 0.5,
        "feature_set": {"velocity": 3, "novelty": 0.7},
    }
    return ReasonerContext(**{**defaults, **overrides})


# ---------------------------------------------------------------------------
# LabelType
# ---------------------------------------------------------------------------


def test_label_type_numerical_value():
    assert LabelType.NUMERICAL == "NUMERICAL"


def test_label_type_categorical_value():
    assert LabelType.CATEGORICAL == "CATEGORICAL"


# ---------------------------------------------------------------------------
# ReasonerContext — construction
# ---------------------------------------------------------------------------


def test_reasoner_context_numerical_label_accepts_float():
    ctx = _make_reasoner_context(label_type=LabelType.NUMERICAL, label_value=0.87)
    assert ctx.label_value == 0.87


def test_reasoner_context_categorical_label_accepts_string():
    ctx = _make_reasoner_context(
        label_type=LabelType.CATEGORICAL,
        label_name="fraud_label",
        label_value="FRAUD",
    )
    assert ctx.label_value == "FRAUD"


def test_reasoner_context_feature_set_accepts_mixed_types():
    ctx = _make_reasoner_context(
        feature_set={
            "velocity": 3,
            "novelty": 0.7,
            "impossible_travel": True,
            "country": "US",
        }
    )
    assert ctx.feature_set["velocity"] == 3
    assert ctx.feature_set["impossible_travel"] is True
    assert ctx.feature_set["country"] == "US"


def test_reasoner_context_attribution_is_optional():
    ctx = _make_reasoner_context(attribution=None)
    assert ctx.attribution is None


def test_reasoner_context_inference_latency_accepts_zero():
    ctx = _make_reasoner_context(inference_latency_ms=0.0)
    assert ctx.inference_latency_ms == 0.0


def test_reasoner_context_inference_latency_rejects_negative():
    with pytest.raises(ValidationError):
        _make_reasoner_context(inference_latency_ms=-0.1)


def test_reasoner_context_carries_reasoner_identity():
    ctx = _make_reasoner_context(
        reasoner_id="ato-reasoner",
        reasoner_name="ATO Reasoner",
        model_version="xgb-v1.2.0",
    )
    assert ctx.reasoner_id == "ato-reasoner"
    assert ctx.reasoner_name == "ATO Reasoner"
    assert ctx.model_version == "xgb-v1.2.0"


# ---------------------------------------------------------------------------
# ReasonerContext — immutability
# ---------------------------------------------------------------------------


def test_reasoner_context_is_immutable():
    ctx = _make_reasoner_context()
    with pytest.raises(ValidationError):
        ctx.label_value = 0.99


# ---------------------------------------------------------------------------
# AttributionSummary
# ---------------------------------------------------------------------------


def test_attribution_summary_all_none_is_valid():
    summary = AttributionSummary()
    assert summary.observation_signals is None
    assert summary.feature_importance is None
    assert summary.narrative is None


def test_attribution_summary_with_observation_signals():
    signal = Signal(feature_name="velocity_1min", shap_value=0.31, raw_value=5.0)
    summary = AttributionSummary(observation_signals=[signal])
    assert len(summary.observation_signals) == 1
    assert summary.observation_signals[0].feature_name == "velocity_1min"


def test_attribution_summary_with_feature_importance():
    importance = {"velocity_1min": 0.45, "device_novelty": 0.30, "geo_novelty": 0.15}
    summary = AttributionSummary(feature_importance=importance)
    assert summary.feature_importance["velocity_1min"] == 0.45


def test_attribution_summary_with_narrative():
    summary = AttributionSummary(
        narrative="Device novelty is the primary driver; model has AAL2 context."
    )
    assert "Device novelty" in summary.narrative


def test_attribution_summary_accepts_all_fields_simultaneously():
    signal = Signal(feature_name="geo_novelty", shap_value=0.22, raw_value=1.0)
    summary = AttributionSummary(
        observation_signals=[signal],
        feature_importance={"geo_novelty": 0.25},
        narrative="New country access is the dominant signal.",
    )
    assert summary.observation_signals is not None
    assert summary.feature_importance is not None
    assert summary.narrative is not None


def test_attribution_summary_is_immutable():
    summary = AttributionSummary(narrative="test")
    with pytest.raises(ValidationError):
        summary.narrative = "modified"


# ---------------------------------------------------------------------------
# ReasonerContext with attribution
# ---------------------------------------------------------------------------


def test_reasoner_context_stores_attribution_verbatim(reasoner_context):
    assert reasoner_context.attribution is not None
    assert reasoner_context.attribution.observation_signals is not None
    assert len(reasoner_context.attribution.observation_signals) == 1
