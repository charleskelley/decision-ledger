"""Behavioral tests for GateRouting and ScorerOutput contracts.

Key contracts:
- risk_score is bounded to [0.0, 1.0].
- inference_latency_ms is non-negative.
- Signal.shap_value is signed — negative values are valid and meaningful
  (they decrease predicted risk). This is an intentional contract, not an
  oversight.
- ScorerOutput and Signal are immutable once constructed.

ScorerOutput is an ATO domain type (reasoner/account_takeover/scorer.py).
Signal is a framework type (core/observation/reasoner_context.py).
"""

import pytest
from pydantic import ValidationError

from core.gate import GateRouting
from core.observation import Signal
from reasoner.account_takeover.scorer import ScorerOutput


def _make_scorer_output(**overrides) -> ScorerOutput:
    """Construct a minimal valid ScorerOutput with optional field overrides."""
    from uuid import uuid4

    defaults = {
        "entity_id": uuid4(),
        "risk_score": 0.5,
        "top_signals": [],
        "scorer_version": "xgb-v1.0.0",
        "inference_latency_ms": 1.0,
        "routing": GateRouting.ROUTE_TO_GATE,
    }
    return ScorerOutput(**{**defaults, **overrides})


def test_risk_score_rejects_values_above_one():
    with pytest.raises(ValidationError):
        _make_scorer_output(risk_score=1.001)


def test_risk_score_rejects_negative_values():
    with pytest.raises(ValidationError):
        _make_scorer_output(risk_score=-0.01)


def test_risk_score_accepts_boundary_values():
    assert _make_scorer_output(risk_score=0.0).risk_score == 0.0
    assert _make_scorer_output(risk_score=1.0).risk_score == 1.0


def test_inference_latency_rejects_negative_values():
    with pytest.raises(ValidationError):
        _make_scorer_output(inference_latency_ms=-0.1)


def test_inference_latency_accepts_zero():
    assert _make_scorer_output(inference_latency_ms=0.0).inference_latency_ms == 0.0


def test_scorer_output_is_immutable(scorer_output):
    with pytest.raises(ValidationError):
        scorer_output.risk_score = 0.99


def test_scorer_output_accepts_empty_signals():
    output = _make_scorer_output(top_signals=[])
    assert output.top_signals == []


def test_scorer_output_gate_routing_values():
    for routing in GateRouting:
        assert _make_scorer_output(routing=routing).routing == routing


def test_all_three_gate_routing_values_exist():
    assert GateRouting.FAST_PATH_ALLOW == "FAST_PATH_ALLOW"
    assert GateRouting.FAST_PATH_BLOCK == "FAST_PATH_BLOCK"
    assert GateRouting.ROUTE_TO_GATE == "ROUTE_TO_GATE"


def test_fast_path_routing_values_are_distinct_from_route_to_gate():
    fast_path = {GateRouting.FAST_PATH_ALLOW, GateRouting.FAST_PATH_BLOCK}
    assert GateRouting.ROUTE_TO_GATE not in fast_path


def test_signal_shap_value_may_be_negative():
    # Negative SHAP values are intentional — they represent features that
    # decrease predicted risk (e.g., known device, low velocity). Rejecting
    # negative values would silently discard risk-reducing signal attribution.
    signal = Signal(feature_name="device_novelty", shap_value=-0.12, raw_value=0.0)
    assert signal.shap_value == -0.12


def test_signal_shap_value_may_be_positive():
    signal = Signal(feature_name="velocity_1min", shap_value=0.31, raw_value=12.0)
    assert signal.shap_value == 0.31


def test_signal_is_immutable(signal):
    with pytest.raises(ValidationError):
        signal.shap_value = 0.99
