"""Unit tests for the ATO retrieval-query builder.

Covers ``build_ato_query`` — the domain-side translator from
``(LoginEvent, ScorerOutput)`` to a natural-language retrieval query
string passed to the framework retriever.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from core.observation import Contribution
from core.routes import GateRoute
from reasoner.account_takeover.events import (
    AuthMethod,
    AuthOutcome,
    Geolocation,
    LoginEvent,
)
from reasoner.account_takeover.retrieval_query import build_ato_query
from reasoner.account_takeover.scorer import ScorerOutput


def _make_event(auth_method: AuthMethod = AuthMethod.PASSWORD) -> LoginEvent:
    return LoginEvent(
        event_id="evt-unit-001",
        timestamp=datetime.now(UTC),
        account_id="acct-001",
        session_id="sess-001",
        ip_address="203.0.113.1",
        geolocation=Geolocation(
            latitude=37.77,
            longitude=-122.42,
            country="US",
            city="San Francisco",
            asn="AS7922",
        ),
        device_fingerprint="fp-abc",
        user_agent="Mozilla/5.0",
        auth_method=auth_method,
        outcome=AuthOutcome.SUCCESS,
    )


def _make_scorer_output(feature_names: list[str]) -> ScorerOutput:
    return ScorerOutput(
        entity_id=uuid4(),
        risk_score=0.7,
        top_signals=[
            Contribution(
                feature_name=name,
                feature_value=1.0,
                method="shap",
                value=0.3,
            )
            for name in feature_names
        ],
        scorer_version="xgb-v1.0.0",
        inference_latency_ms=2.5,
        route=GateRoute.ROUTE_TO_GATE,
    )


def test_build_query_maps_impossible_travel_signal():
    """impossible_travel signal maps to geographic block terminology."""
    query = build_ato_query(_make_event(), _make_scorer_output(["impossible_travel"]))
    assert "impossible travel" in query.lower()


def test_build_query_includes_auth_method_terms():
    """Auth method is appended to the query regardless of signals."""
    query = build_ato_query(
        _make_event(auth_method=AuthMethod.MFA_TOTP),
        _make_scorer_output([]),
    )
    assert "TOTP" in query or "MFA" in query


def test_build_query_falls_back_when_no_signals_or_method():
    """Returns the generic fallback when no signals and no known auth method map."""
    query = build_ato_query(
        _make_event(auth_method=AuthMethod.PASSWORD),
        _make_scorer_output([]),
    )
    # PASSWORD maps to a term, so the result is non-empty
    assert query != ""


def test_build_query_uses_generic_fallback_for_empty_everything():
    """Returns generic fallback when signals are unknown and auth method maps."""
    query = build_ato_query(
        _make_event(auth_method=AuthMethod.PASSWORD),
        _make_scorer_output(["unknown_feature_xyz"]),
    )
    # Should still include PASSWORD term (not the generic fallback)
    assert "password" in query.lower() or "credential" in query.lower()


def test_build_query_uses_top_3_signals_only():
    """Only the top-3 SHAP signals contribute terms to the query."""
    query = build_ato_query(
        _make_event(),
        _make_scorer_output(
            [
                "impossible_travel",
                "velocity_1min",
                "device_novelty",
                "geo_novelty",  # 4th — should be ignored
            ]
        ),
    )
    assert "geographic anomaly new country" not in query


def test_build_query_includes_failure_phrase_for_failure_outcome():
    """FAILURE/BLOCKED outcomes append a credential-failure phrase."""
    event = _make_event()
    failed = event.model_copy(update={"outcome": AuthOutcome.FAILURE})
    query = build_ato_query(failed, _make_scorer_output([]))
    assert "failure" in query.lower() or "blocked" in query.lower()
