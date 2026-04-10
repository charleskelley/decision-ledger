"""Shared fixtures for reasoner/ domain tests.

All fixtures produce valid, fully-constructed domain objects. Tests that
need edge-case variants should build from these via model_copy() or by
constructing directly with the specific deviation under test.
"""

from datetime import UTC, datetime

import pytest

from reasoner.account_takeover.events import (
    AuthMethod,
    AuthOutcome,
    Geolocation,
    LoginEvent,
)
from reasoner.account_takeover.features import AtoFeatureVector, WindowSpec


@pytest.fixture
def geo_data():
    return Geolocation(
        latitude=37.7749,
        longitude=-122.4194,
        country="US",
        city="San Francisco",
        asn="AS7922",
    )


@pytest.fixture
def login_event(geo_data):
    return LoginEvent(
        event_id="evt-test-001",
        timestamp=datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
        account_id="acc-12345",
        session_id="sess-test-001",
        ip_address="192.168.1.100",
        geolocation=geo_data,
        device_fingerprint="fp-abc123",
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        auth_method=AuthMethod.PASSWORD,
        outcome=AuthOutcome.SUCCESS,
    )


@pytest.fixture
def ato_feature_vector():
    return AtoFeatureVector(
        computed_at=datetime(2024, 1, 15, 10, 30, 1, tzinfo=UTC),
        account_id="acc-12345",
        sparse_history=False,
        velocity_1min=1,
        velocity_5min=3,
        velocity_60min=8,
        velocity_1440min=22,
        ip_novelty=0.0,
        device_novelty=0.0,
        geo_novelty=0.0,
        impossible_travel=False,
        travel_speed_kmh=None,
        device_consistency_score=0.95,
        user_agent_consistency=0.98,
        windows=[
            WindowSpec(window_minutes=1, available=True, event_count=1),
            WindowSpec(window_minutes=5, available=True, event_count=3),
        ],
    )
