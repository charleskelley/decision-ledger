"""Tests for app/decide.py — pure pipeline orchestration.

The route handler in app/main.py and the eval harness's PipelineDriver
both delegate to ``execute_pipeline()``. These tests lock in the
orchestration contract: which services are called, in which order,
with which arguments. Downstream artifact construction (build_bundle,
resolve, build_observation) is exercised by their own dedicated test
files; we don't re-test that here.

Fast-path coverage only — the gate-path branch (``ROUTE_TO_GATE``)
is covered end-to-end by the Step 5 scenario smoke test against the
real stack.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.decide import execute_pipeline
from core.routes import GateRoute
from reasoner.account_takeover.events import (
    AuthMethod,
    AuthOutcome,
    Geolocation,
    LoginEvent,
)
from reasoner.account_takeover.features import AtoFeatureVector, WindowSpec
from reasoner.account_takeover.scorer import ScorerOutput

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_WINDOWS = [
    WindowSpec(window_minutes=1, available=True, event_count=1),
    WindowSpec(window_minutes=5, available=True, event_count=2),
    WindowSpec(window_minutes=60, available=True, event_count=5),
    WindowSpec(window_minutes=1440, available=True, event_count=15),
]


def _make_event(event_id: str = "evt-decide-001") -> LoginEvent:
    return LoginEvent(
        event_id=event_id,
        timestamp=datetime.now(UTC),
        account_id="acct-decide",
        session_id="sess-decide",
        ip_address="203.0.113.10",
        geolocation=Geolocation(
            latitude=37.77,
            longitude=-122.42,
            country="US",
            city="San Francisco",
            asn="AS7922",
        ),
        device_fingerprint="fp-decide",
        user_agent="Mozilla/5.0",
        auth_method=AuthMethod.PASSWORD,
        outcome=AuthOutcome.SUCCESS,
    )


def _make_features() -> AtoFeatureVector:
    return AtoFeatureVector(
        account_id="acct-decide",
        computed_at=datetime.now(UTC),
        sparse_history=False,
        velocity_1min=1,
        velocity_5min=2,
        velocity_60min=5,
        velocity_1440min=15,
        ip_novelty=0.0,
        device_novelty=0.0,
        geo_novelty=0.0,
        impossible_travel=False,
        travel_speed_kmh=None,
        device_consistency_score=0.95,
        user_agent_consistency=0.98,
        windows=_DEFAULT_WINDOWS,
    )


def _make_fast_path_scorer_output() -> ScorerOutput:
    """Low-risk score → assembler routes to FAST_PATH_ALLOW."""
    return ScorerOutput(
        entity_id=uuid4(),
        risk_score=0.05,
        top_signals=[],
        scorer_version="xgb-test",
        inference_latency_ms=1.0,
        route=GateRoute.FAST_PATH_ALLOW,
    )


@pytest.fixture
def stub_services() -> dict[str, MagicMock]:
    """Build stub services that produce realistic Pydantic instances.

    feature_svc and scorer return real domain objects so build_observation
    can run against them. retriever, gate, and store are MagicMocks
    (no calls expected on the fast path).
    """
    feature_svc = MagicMock()
    feature_svc.compute.return_value = _make_features()

    scorer = MagicMock()
    scorer.score.return_value = _make_fast_path_scorer_output()

    retriever = MagicMock()
    retriever.build_query.return_value = "test query"

    # gate.evaluate is async (DR-23) — use AsyncMock so awaits work
    gate = MagicMock()
    gate.evaluate = AsyncMock()
    store = MagicMock()

    return {
        "feature_svc": feature_svc,
        "scorer": scorer,
        "retriever": retriever,
        "gate": gate,
        "store": store,
    }


async def _run(
    stub_services: dict[str, MagicMock],
    *,
    event: LoginEvent | None = None,
    decision_id: str | None = None,
    idempotency_key: str | None = None,
) -> Callable:
    """Invoke execute_pipeline with the stub services and an event."""
    return await execute_pipeline(
        event=event or _make_event(),
        feature_svc=stub_services["feature_svc"],
        scorer=stub_services["scorer"],
        retriever=stub_services["retriever"],
        gate=stub_services["gate"],
        store=stub_services["store"],
        corpus_version="corpus-test",
        decision_id=decision_id,
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fast_path_does_not_invoke_retriever_or_gate(stub_services):
    """ROUTE != ROUTE_TO_GATE → retriever.retrieve and gate.evaluate untouched."""
    await _run(stub_services)

    assert stub_services["retriever"].retrieve.call_count == 0
    assert stub_services["gate"].evaluate.call_count == 0


@pytest.mark.asyncio
async def test_fast_path_returns_bundle_with_no_gate_artifacts(stub_services):
    """Fast-path bundle has gate_input and gate_output both None."""
    bundle = await _run(stub_services)

    assert bundle.gate_input is None
    assert bundle.gate_output is None
    assert bundle.retrieval_path == "skipped"


@pytest.mark.asyncio
async def test_persists_bundle_via_store_write(stub_services):
    """The returned bundle is the same object passed to store.write."""
    bundle = await _run(stub_services)

    assert stub_services["store"].write.call_count == 1
    persisted = stub_services["store"].write.call_args[0][0]
    assert persisted is bundle


@pytest.mark.asyncio
async def test_uses_provided_decision_id(stub_services):
    """An explicit decision_id flows through to the persisted bundle."""
    bundle = await _run(stub_services, decision_id="custom-decision-id")

    assert bundle.decision_id == "custom-decision-id"


@pytest.mark.asyncio
async def test_defaults_decision_id_to_generated_uuid(stub_services):
    """When omitted, decision_id is auto-generated and non-empty."""
    bundle = await _run(stub_services)

    assert bundle.decision_id
    assert len(bundle.decision_id) >= 32  # UUID4 string form


@pytest.mark.asyncio
async def test_defaults_idempotency_key_to_event_id(stub_services):
    """When omitted, idempotency_key falls back to event.event_id."""
    event = _make_event(event_id="evt-fallback-key")
    bundle = await _run(stub_services, event=event)

    assert bundle.idempotency_key == "evt-fallback-key"


@pytest.mark.asyncio
async def test_uses_provided_idempotency_key(stub_services):
    """An explicit idempotency_key takes precedence over event.event_id."""
    bundle = await _run(stub_services, idempotency_key="explicit-key")

    assert bundle.idempotency_key == "explicit-key"


@pytest.mark.asyncio
async def test_populates_latency_breakdown_for_each_phase(stub_services):
    """Bundle latency_breakdown carries one entry per measurable phase.

    bundle_ms is intentionally not tracked — see app/decide.py for why.
    """
    bundle = await _run(stub_services)

    expected_phases = {
        "features_ms",
        "scorer_ms",
        "retrieval_ms",
        "gate_ms",
        "enforcement_ms",
    }
    assert expected_phases.issubset(bundle.latency_breakdown.keys())
    # Fast path: retrieval and gate phases recorded as 0.0
    assert bundle.latency_breakdown["retrieval_ms"] == 0.0
    assert bundle.latency_breakdown["gate_ms"] == 0.0


@pytest.mark.asyncio
async def test_calls_feature_svc_with_event(stub_services):
    """feature_svc.compute receives the input event verbatim."""
    event = _make_event(event_id="evt-feature-check")
    await _run(stub_services, event=event)

    assert stub_services["feature_svc"].compute.call_count == 1
    assert stub_services["feature_svc"].compute.call_args[0][0] is event


@pytest.mark.asyncio
async def test_calls_scorer_with_features(stub_services):
    """scorer.score receives the AtoFeatureVector from feature_svc.compute."""
    features = stub_services["feature_svc"].compute.return_value
    await _run(stub_services)

    assert stub_services["scorer"].score.call_count == 1
    assert stub_services["scorer"].score.call_args[0][0] is features
