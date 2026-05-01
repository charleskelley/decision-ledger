"""Shared fixtures for app/ layer tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.actions import DecisionAction
from core.gate.policy import Citation, PolicyGateVerdict
from core.observation import (
    AttributionSummary,
    Contribution,
    GateContext,
    LabelType,
    ReasonerContext,
)
from core.routes import GateRoute
from reasoner.account_takeover.events import (
    AuthMethod,
    AuthOutcome,
    Geolocation,
    LoginEvent,
)


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
def gate_context():
    return GateContext(
        gate_id="policy",
        gate_config={
            "template_id": "ato-v1",
            "template_vars": {
                "risk_score": "0.150",
                "risk_tier": "STANDARD",
                "auth_method": "PASSWORD",
                "outcome": "SUCCESS",
                "jurisdiction": "US",
                "top_signals": "velocity_1min=1.0 (SHAP +0.050)",
                "impossible_travel": "False",
                "velocity_1min": "1",
                "velocity_5min": "3",
                "velocity_60min": "8",
                "device_novelty": "0.00",
                "ip_novelty": "0.00",
                "geo_novelty": "0.00",
                "sparse_history": "False",
            },
            "jurisdictions": ["US_FEDERAL", "INTERNAL"],
            "risk_tier": "STANDARD",
        },
    )


@pytest.fixture
def signal():
    return Contribution(
        feature_name="velocity_1min",
        feature_value=1.0,
        method="shap",
        value=0.05,
    )


@pytest.fixture
def reasoner_context(signal):
    return ReasonerContext(
        reasoner_id="ato-reasoner",
        reasoner_name="ATO Reasoner",
        model_version="xgb-v1.0.0",
        inference_latency_ms=2.5,
        label_type=LabelType.NUMERICAL,
        label_name="risk_score",
        label_value=0.15,
        feature_set={
            "velocity_1min": 1,
            "velocity_5min": 3,
            "velocity_60min": 8,
            "velocity_1440min": 22,
            "ip_novelty": 0.0,
            "device_novelty": 0.0,
            "geo_novelty": 0.0,
            "impossible_travel": False,
            "travel_speed_kmh": "N/A",
            "device_consistency_score": 0.95,
            "user_agent_consistency": 0.98,
            "sparse_history": False,
        },
        attribution=AttributionSummary(feature_contributions=[signal]),
    )


@pytest.fixture
def login_event(geo_data, gate_context, reasoner_context):
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
        route=GateRoute.FAST_PATH_ALLOW,
        reasoner_context=reasoner_context,
        fast_path_rationale="risk_score=0.150 < 0.20 → FAST_PATH_ALLOW",
        gate_context=gate_context,
    )


@pytest.fixture
def citation():
    return Citation(
        policy_id="NIST-800-63B",
        snippet=(
            "Verifiers SHALL require subscribers to use a multi-factor authenticator."
        ),
        relevance="Directly supports the MFA requirement at AAL2.",
    )


@pytest.fixture
def gate_output(citation):
    return PolicyGateVerdict(
        permitted_actions=[DecisionAction.ALLOW],
        required_controls=[],
        rationale="Low risk profile. No anomalous signals. Known device and IP.",
        citations=[citation],
        confidence=0.92,
        escalate_to_human=False,
        escalation_reason=None,
    )
