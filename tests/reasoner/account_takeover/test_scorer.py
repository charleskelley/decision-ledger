"""Behavioral tests for ScorerOutput contracts.

ScorerOutput is an ATO domain type (reasoner/account_takeover/scorer.py).
Key contracts:
- risk_score is bounded to [0.0, 1.0].
- inference_latency_ms is non-negative.
- ScorerOutput is immutable once constructed.
- route accepts all GateRoute values.
"""

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from core.routes import GateRoute
from reasoner.account_takeover.scorer import ScorerOutput


def _make_scorer_output(**overrides: Any) -> ScorerOutput:
    """Construct a minimal valid ScorerOutput with optional field overrides."""
    defaults: dict[str, Any] = {
        "entity_id": uuid4(),
        "risk_score": 0.5,
        "top_signals": [],
        "scorer_version": "xgb-v1.0.0",
        "inference_latency_ms": 1.0,
        "route": GateRoute.ROUTE_TO_GATE,
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


def test_scorer_output_is_immutable():
    output = _make_scorer_output()
    with pytest.raises(ValidationError):
        output.risk_score = 0.99


def test_scorer_output_accepts_empty_signals():
    output = _make_scorer_output(top_signals=[])
    assert output.top_signals == []


def test_scorer_output_routing_accepts_all_gate_routing_values():
    for route in GateRoute:
        assert _make_scorer_output(route=route).route == route
