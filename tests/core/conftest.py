"""Shared fixtures for core/ contract tests.

All fixtures produce valid, fully-constructed domain objects. Tests that
need edge-case variants should build from these via model_copy() or by
constructing directly with the specific deviation under test.
"""

from datetime import UTC, datetime

import pytest

from core.actions import DecisionAction
from core.bundle import PolicySnippet, ReviewPacket, TokenCost
from core.gate import Citation, GateRouting, PolicyGateOutput, PromptSnapshot
from core.observation import (
    AttributionSummary,
    GateContext,
    LabelType,
    ReasonerContext,
    Signal,
)
from reasoner.account_takeover.events import (
    AuthMethod,
    AuthOutcome,
    Geolocation,
    LoginEvent,
)
from reasoner.account_takeover.features import AtoFeatureVector, WindowSpec
from reasoner.account_takeover.scorer import ScorerOutput


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
        prompt_template_id="ato-v1",
        jurisdictions=["US_FEDERAL", "INTERNAL"],
        risk_tier="STANDARD",
        template_vars={
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
        attribution=AttributionSummary(observation_signals=[signal]),
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
        routing=GateRouting.FAST_PATH_ALLOW,
        reasoner_context=reasoner_context,
        fast_path_rationale="risk_score=0.150 < 0.20 → FAST_PATH_ALLOW",
        gate_context=gate_context,
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


@pytest.fixture
def signal():
    return Signal(
        feature_name="velocity_1min",
        shap_value=0.05,
        raw_value=1.0,
    )


@pytest.fixture
def scorer_output(login_event, signal):
    return ScorerOutput(
        entity_id=login_event.entity_id,
        risk_score=0.15,
        top_signals=[signal],
        scorer_version="xgb-v1.0.0",
        inference_latency_ms=2.5,
        routing=GateRouting.FAST_PATH_ALLOW,
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
def prompt_snapshot():
    return PromptSnapshot(
        template_id="ato-v1",
        version="1.0.0",
        template_str=(
            "You are a policy gate. Risk score: {risk_score}. "
            "Policy context:\n{policy_snippets}"
        ),
    )


@pytest.fixture
def policy_gate_output(citation):
    return PolicyGateOutput(
        permitted_actions=[DecisionAction.ALLOW],
        required_controls=[],
        rationale=(
            "Low risk profile. No anomalous signals detected. Known device and IP."
        ),
        citations=[citation],
        confidence=0.92,
        escalate_to_human=False,
        escalation_reason=None,
    )


@pytest.fixture
def policy_snippet():
    return PolicySnippet(
        policy_id="NIST-800-63B",
        title="Digital Identity Guidelines: Authentication",
        version="4.0",
        jurisdiction="US_FEDERAL",
        section_path="5.2.3 — Authenticator Assurance Level 2",
        text="AAL2 authentication SHALL use a multi-factor authenticator.",
        relevance_score=0.91,
        retrieval_path="reranked",
    )


@pytest.fixture
def review_packet(login_event):
    return ReviewPacket(
        decision_id="dec-test-001",
        entity_id=login_event.entity_id,
        created_at=datetime(2024, 1, 15, 10, 30, 2, tzinfo=UTC),
        hold_reason="Low confidence gate output with new device signal.",
        enforcement_rule=None,
        risk_score=0.61,
        top_signals=[
            Signal(feature_name="device_novelty", shap_value=0.22, raw_value=1.0)
        ],
        priority=0,
    )


@pytest.fixture
def token_cost():
    return TokenCost(
        prompt_tokens=1240,
        completion_tokens=312,
        total_tokens=1552,
        cost_usd=0.0078,
        model="gpt-4o-2024-08-06",
    )
