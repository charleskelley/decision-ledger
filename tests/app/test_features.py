"""Tests for app/features/features.py — pure function and FeatureService."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.features.features import (
    _MAX_TRAVEL_SPEED_KMH,
    _SPARSE_HISTORY_THRESHOLD,
    FeatureService,
    _AccountHistory,
    _compute_features,
    _haversine_km,
)

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_history(**overrides) -> _AccountHistory:
    """Build an _AccountHistory with sensible defaults merged with overrides.

    Args:
        **overrides: Field values to override on the default history.

    Returns:
        An _AccountHistory instance suitable for use in tests.
    """
    defaults: dict = {
        "velocity_1min": 1,
        "velocity_5min": 3,
        "velocity_60min": 8,
        "velocity_1440min": 22,
        "ip_seen": True,
        "ip_count": 2,
        "device_seen": True,
        "device_count": 1,
        "country_seen": True,
        "ua_seen": True,
        "ua_count": 1,
        "last_lat": None,
        "last_lon": None,
        "last_ts": None,
        "total_events": 20,
    }
    defaults.update(overrides)
    return _AccountHistory(**defaults)


# ---------------------------------------------------------------------------
# Haversine tests
# ---------------------------------------------------------------------------


def test_haversine_same_point_returns_zero():
    assert _haversine_km(37.7749, -122.4194, 37.7749, -122.4194) == pytest.approx(
        0.0, abs=1e-6
    )


def test_haversine_sf_to_ny_approx():
    # San Francisco → New York City ≈ 4130 km
    km = _haversine_km(37.7749, -122.4194, 40.7128, -74.0060)
    assert abs(km - 4130.0) < 50.0


# ---------------------------------------------------------------------------
# _compute_features pure tests
# ---------------------------------------------------------------------------


def test_known_account_known_ip_has_zero_ip_novelty(login_event):
    history = _make_history(ip_seen=True)
    now_ts = login_event.timestamp.timestamp()
    fv = _compute_features(login_event, history, now_ts)
    assert fv.ip_novelty == pytest.approx(0.0)


def test_known_account_new_ip_has_full_ip_novelty(login_event):
    history = _make_history(ip_seen=False)
    now_ts = login_event.timestamp.timestamp()
    fv = _compute_features(login_event, history, now_ts)
    assert fv.ip_novelty == pytest.approx(1.0)


def test_new_account_has_zero_point_five_novelty(login_event):
    history = _make_history(total_events=0)
    now_ts = login_event.timestamp.timestamp()
    fv = _compute_features(login_event, history, now_ts)
    assert fv.ip_novelty == pytest.approx(0.5)
    assert fv.device_novelty == pytest.approx(0.5)
    assert fv.geo_novelty == pytest.approx(0.5)


def test_impossible_travel_detected(login_event):
    # Place last location in Tokyo — far from San Francisco in the fixture
    now_ts = login_event.timestamp.timestamp()
    # 1 second ago in Tokyo (35.68, 139.69) vs SF (37.77, -122.42)
    history = _make_history(
        last_lat=35.6895,
        last_lon=139.6917,
        last_ts=now_ts - 1.0,  # 1 second ago → impossible speed
    )
    fv = _compute_features(login_event, history, now_ts)
    assert fv.impossible_travel is True
    assert fv.travel_speed_kmh is not None
    assert fv.travel_speed_kmh > _MAX_TRAVEL_SPEED_KMH


def test_no_prior_location_gives_none_travel_speed(login_event):
    history = _make_history(last_lat=None, last_lon=None, last_ts=None)
    now_ts = login_event.timestamp.timestamp()
    fv = _compute_features(login_event, history, now_ts)
    assert fv.travel_speed_kmh is None
    assert fv.impossible_travel is False


def test_sparse_history_when_below_threshold(login_event):
    history = _make_history(total_events=5)
    assert _SPARSE_HISTORY_THRESHOLD > 5
    now_ts = login_event.timestamp.timestamp()
    fv = _compute_features(login_event, history, now_ts)
    assert fv.sparse_history is True


def test_not_sparse_when_above_threshold(login_event):
    history = _make_history(total_events=15)
    assert _SPARSE_HISTORY_THRESHOLD <= 15
    now_ts = login_event.timestamp.timestamp()
    fv = _compute_features(login_event, history, now_ts)
    assert fv.sparse_history is False


def test_device_consistency_seen(login_event):
    history = _make_history(device_seen=True)
    now_ts = login_event.timestamp.timestamp()
    fv = _compute_features(login_event, history, now_ts)
    assert fv.device_consistency_score == pytest.approx(0.95)


def test_device_consistency_new(login_event):
    history = _make_history(device_count=3, device_seen=False)
    now_ts = login_event.timestamp.timestamp()
    fv = _compute_features(login_event, history, now_ts)
    assert fv.device_consistency_score == pytest.approx(0.05)


def test_windows_have_correct_counts(login_event):
    history = _make_history(
        velocity_1min=2,
        velocity_5min=4,
        velocity_60min=9,
        velocity_1440min=30,
    )
    now_ts = login_event.timestamp.timestamp()
    fv = _compute_features(login_event, history, now_ts)
    by_minutes = {w.window_minutes: w.event_count for w in fv.windows}
    assert by_minutes[1] == 2
    assert by_minutes[5] == 4
    assert by_minutes[60] == 9
    assert by_minutes[1440] == 30


def test_feature_vector_entity_id_matches_account_id(login_event):
    import uuid

    from reasoner.account_takeover.events import _ATO_ACCOUNT_NS

    history = _make_history()
    now_ts = login_event.timestamp.timestamp()
    fv = _compute_features(login_event, history, now_ts)
    expected = uuid.uuid5(_ATO_ACCOUNT_NS, login_event.account_id)
    assert fv.entity_id == expected


# ---------------------------------------------------------------------------
# FeatureService.compute() mock tests
# ---------------------------------------------------------------------------


def _build_mock_redis(read_results: list) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Build a mock Redis client with two pipeline call-side effects.

    Args:
        read_results: The 14-item list returned by the read pipeline execute().

    Returns:
        A tuple of (mock_redis, mock_read_pipeline, mock_write_pipeline).
    """
    mock_redis = MagicMock()
    mock_rp = MagicMock()
    mock_wp = MagicMock()
    mock_redis.pipeline.side_effect = [mock_rp, mock_wp]
    mock_rp.execute.return_value = read_results
    mock_wp.execute.return_value = [None] * 13
    return mock_redis, mock_rp, mock_wp


def test_feature_service_velocities_from_pipeline(login_event):
    read_results = [
        None,  # 0: zremrangebyscore
        2,  # 1: 1min
        5,  # 2: 5min
        8,  # 3: 60min
        15,  # 4: 1440min
        True,  # 5: ip sismember
        3,  # 6: ip scard
        False,  # 7: device sismember
        1,  # 8: device scard
        True,  # 9: country sismember
        False,  # 10: ua sismember
        1,  # 11: ua scard
        {},  # 12: last_geo hgetall
        b"20",  # 13: total get
    ]
    mock_redis, _, _ = _build_mock_redis(read_results)
    service = FeatureService(mock_redis)
    fv = service.compute(login_event)

    assert fv.velocity_1min == 2
    assert fv.velocity_5min == 5
    assert fv.velocity_60min == 8
    assert fv.velocity_1440min == 15


def test_feature_service_writes_back_to_redis(login_event):
    read_results = [
        None,
        1,
        3,
        8,
        22,
        True,
        2,
        True,
        1,
        True,
        True,
        1,
        {},
        b"20",
    ]
    mock_redis, _, mock_wp = _build_mock_redis(read_results)
    service = FeatureService(mock_redis)
    service.compute(login_event)

    mock_wp.execute.assert_called_once()


def test_feature_service_new_account_sparse_history(login_event):
    read_results = [
        None,
        0,
        0,
        0,
        0,
        False,
        0,
        False,
        0,
        False,
        False,
        0,
        {},
        None,
    ]
    mock_redis, _, _ = _build_mock_redis(read_results)
    service = FeatureService(mock_redis)
    fv = service.compute(login_event)

    assert fv.sparse_history is True


def test_feature_service_last_geo_parsed_from_bytes_dict(login_event):
    # last_geo in Tokyo — very far from SF fixture lat/lon, near-zero elapsed
    now_ts = login_event.timestamp.timestamp()
    read_results = [
        None,
        1,
        1,
        2,
        5,
        True,
        1,
        True,
        1,
        True,
        True,
        1,
        {b"lat": b"35.6895", b"lon": b"139.6917", b"ts": str(now_ts - 1.0).encode()},
        b"20",
    ]
    mock_redis, _, _ = _build_mock_redis(read_results)
    service = FeatureService(mock_redis)
    fv = service.compute(login_event)

    # Travel speed should be set and be very high (impossible travel)
    assert fv.travel_speed_kmh is not None
    assert fv.impossible_travel is True
