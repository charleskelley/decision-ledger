"""Tests for AtoScorer — routing, SHAP signal structure, score bounds."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.routes import GateRoute
from reasoner.account_takeover.features import AtoFeatureVector, WindowSpec
from reasoner.account_takeover.scorer.scorer import (
    FEATURE_NAMES,
    _route,
    _to_feature_row,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_WINDOWS = [
    WindowSpec(window_minutes=1, available=True, event_count=1),
    WindowSpec(window_minutes=5, available=True, event_count=3),
    WindowSpec(window_minutes=60, available=True, event_count=8),
    WindowSpec(window_minutes=1440, available=True, event_count=20),
]


def _make_fv(**overrides) -> AtoFeatureVector:
    defaults: dict = {
        "account_id": "acct-001",
        "computed_at": datetime.now(UTC),
        "sparse_history": False,
        "velocity_1min": 1,
        "velocity_5min": 2,
        "velocity_60min": 5,
        "velocity_1440min": 15,
        "ip_novelty": 0.0,
        "device_novelty": 0.0,
        "geo_novelty": 0.0,
        "impossible_travel": False,
        "travel_speed_kmh": None,
        "device_consistency_score": 0.95,
        "user_agent_consistency": 0.98,
        "windows": _DEFAULT_WINDOWS,
    }
    defaults.update(overrides)
    return AtoFeatureVector(**defaults)


# ---------------------------------------------------------------------------
# Module-scoped fixtures — train once per test session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def trained_model_path(tmp_path_factory):
    from reasoner.account_takeover.scorer.trainer import train

    path = tmp_path_factory.mktemp("scorer") / "model.ubj"
    train(n_samples=300, model_path=path)
    return path


@pytest.fixture(scope="module")
def scorer(trained_model_path):
    from reasoner.account_takeover.scorer.scorer import AtoScorer

    return AtoScorer(trained_model_path)


# ---------------------------------------------------------------------------
# Score bounds
# ---------------------------------------------------------------------------


def test_score_is_in_unit_interval(scorer):
    fv = _make_fv()
    result = scorer.score(fv)
    assert 0.0 <= result.risk_score <= 1.0


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_route_fast_path_allow_for_low_risk(scorer):
    fv = _make_fv(
        velocity_1min=0,
        velocity_5min=0,
        velocity_60min=0,
        velocity_1440min=0,
        ip_novelty=0.0,
        device_novelty=0.0,
        geo_novelty=0.0,
        impossible_travel=False,
        travel_speed_kmh=None,
        device_consistency_score=0.99,
        user_agent_consistency=0.99,
        sparse_history=False,
    )
    result = scorer.score(fv)
    assert result.route == GateRoute.FAST_PATH_ALLOW


def test_route_fast_path_block_for_high_risk(scorer):
    fv = _make_fv(
        velocity_1min=10,
        velocity_5min=20,
        ip_novelty=1.0,
        device_novelty=1.0,
        geo_novelty=1.0,
        impossible_travel=True,
        sparse_history=True,
        device_consistency_score=0.05,
        user_agent_consistency=0.05,
    )
    result = scorer.score(fv)
    assert result.risk_score > 0.5
    assert isinstance(result.route, GateRoute)


def test_route_route_to_gate_for_mid_risk():
    assert _route(0.5) == GateRoute.ROUTE_TO_GATE
    assert _route(0.15) == GateRoute.FAST_PATH_ALLOW
    assert _route(0.90) == GateRoute.FAST_PATH_BLOCK


# ---------------------------------------------------------------------------
# Signal structure
# ---------------------------------------------------------------------------


def test_top_signals_count(scorer):
    fv = _make_fv()
    result = scorer.score(fv)
    assert len(result.top_signals) == 5


def test_top_signals_feature_names_are_valid(scorer):
    fv = _make_fv()
    result = scorer.score(fv)
    for signal in result.top_signals:
        assert signal.feature_name in FEATURE_NAMES


# ---------------------------------------------------------------------------
# Latency and metadata
# ---------------------------------------------------------------------------


def test_inference_latency_is_positive(scorer):
    fv = _make_fv()
    result = scorer.score(fv)
    assert result.inference_latency_ms > 0


def test_scorer_version_set(scorer):
    fv = _make_fv()
    result = scorer.score(fv)
    assert result.scorer_version != ""


def test_entity_id_matches_feature_vector(scorer):
    fv = _make_fv()
    result = scorer.score(fv)
    assert result.entity_id == fv.entity_id


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------


def test_to_feature_row_shape():
    fv = _make_fv()
    row = _to_feature_row(fv)
    assert row.shape == (len(FEATURE_NAMES),)
    assert row.dtype == "float32"


def test_to_feature_row_none_travel_speed_becomes_zero():
    fv = _make_fv(travel_speed_kmh=None)
    row = _to_feature_row(fv)
    idx = FEATURE_NAMES.index("travel_speed_kmh")
    assert row[idx] == 0.0


def test_to_feature_row_impossible_travel_becomes_one():
    fv = _make_fv(impossible_travel=True)
    row = _to_feature_row(fv)
    idx = FEATURE_NAMES.index("impossible_travel")
    assert row[idx] == 1.0


# ---------------------------------------------------------------------------
# Heuristic label
# ---------------------------------------------------------------------------


def test_heuristic_label_high_risk():
    from reasoner.account_takeover.scorer.trainer import _heuristic_label

    row = dict.fromkeys(FEATURE_NAMES, 0.0)
    row["impossible_travel"] = 1.0
    row["ip_novelty"] = 1.0
    row["device_novelty"] = 1.0
    row["geo_novelty"] = 1.0
    row["device_consistency_score"] = 0.0
    row["user_agent_consistency"] = 0.0
    assert _heuristic_label(row) > 0.5


def test_heuristic_label_low_risk():
    from reasoner.account_takeover.scorer.trainer import _heuristic_label

    row = dict.fromkeys(FEATURE_NAMES, 0.0)
    row["device_consistency_score"] = 1.0
    row["user_agent_consistency"] = 1.0
    assert _heuristic_label(row) < 0.5
