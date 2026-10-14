"""Tests for the ATO domain pipeline orchestrator.

The ATO route handler and the eval harness's PipelineDriver both delegate
to ``run_ato_decision()``. These tests lock in the orchestration contract:
which services are called, in which order, with which arguments.
Downstream artifact construction (build_bundle, resolve, build_observation)
is exercised by their own dedicated test files; we don't re-test that here.

Fast-path coverage only — the gate-path branch (``ROUTE_TO_GATE``)
is covered end-to-end by the scenario smoke test against the real stack.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.gates import ComposedGate, GateKind
from app.plugin import FrameworkServices
from app.reasoner_registry import StaticReasonerRegistry
from ato_reasoner.events import (
    AuthMethod,
    AuthOutcome,
    Geolocation,
    LoginEvent,
)
from ato_reasoner.features import AtoFeatureVector, WindowSpec
from ato_reasoner.pipeline import run_ato_decision
from ato_reasoner.plugin import AtoServices
from ato_reasoner.registry import ATO_REGISTRATION
from ato_reasoner.scorer import ScorerOutput
from core.routes import GateRoute

if TYPE_CHECKING:
    from core.bundle import DecisionBundle


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


class _PolicyGatePlugin:
    """Plugin metadata for the composed policy gate double."""

    gate_id = "policy"
    kind = GateKind.LLM
    deterministic = False
    config_model = None

    def build(self, framework: FrameworkServices) -> MagicMock:
        raise AssertionError("the stack composes its gate directly")


class _Stack:
    """The two service objects ``run_ato_decision`` takes, built on stubs.

    feature_svc and scorer return real domain objects so build_observation
    can run against them. The composed policy gate and the store are
    MagicMocks (no gate call expected on the fast path).
    """

    def __init__(self) -> None:
        self.feature_svc = MagicMock()
        self.feature_svc.compute.return_value = _make_features()
        self.scorer = MagicMock()
        self.scorer.score.return_value = _make_fast_path_scorer_output()
        # gate.evaluate is async (DR-23) — use AsyncMock so awaits work
        self.gate = MagicMock()
        self.gate.evaluate = AsyncMock()
        self.store = MagicMock()

        self.services = AtoServices(
            feature_svc=self.feature_svc,
            scorer=self.scorer,
            corpus_version="corpus-test",
            redis=MagicMock(),
        )
        self.framework = FrameworkServices(
            settings=MagicMock(),
            pg_conn=MagicMock(),
            store=self.store,
            registry=StaticReasonerRegistry([ATO_REGISTRATION]),
            gates={"policy": ComposedGate(plugin=_PolicyGatePlugin(), gate=self.gate)},
        )


@pytest.fixture
def stack() -> _Stack:
    return _Stack()


async def _run(
    stack: _Stack,
    *,
    event: LoginEvent | None = None,
    decision_id: str | None = None,
    idempotency_key: str | None = None,
) -> DecisionBundle:
    """Invoke run_ato_decision with the stub services and an event."""
    return await run_ato_decision(
        event=event or _make_event(),
        services=stack.services,
        framework=stack.framework,
        decision_id=decision_id,
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fast_path_does_not_invoke_the_gate(stack):
    """ROUTE != ROUTE_TO_GATE → the composed gate's evaluate is untouched."""
    await _run(stack)

    assert stack.gate.evaluate.call_count == 0


@pytest.mark.asyncio
async def test_fast_path_returns_bundle_with_no_gate_artifacts(stack):
    """Fast-path bundle has gate_input and gate_output both None."""
    bundle = await _run(stack)

    assert bundle.gate_input is None
    assert bundle.gate_output is None


@pytest.mark.asyncio
async def test_persists_bundle_via_store_write(stack):
    """The returned bundle is the same object passed to store.write."""
    bundle = await _run(stack)

    assert stack.store.write.call_count == 1
    persisted = stack.store.write.call_args[0][0]
    assert persisted is bundle


@pytest.mark.asyncio
async def test_uses_provided_decision_id(stack):
    """An explicit decision_id flows through to the persisted bundle."""
    bundle = await _run(stack, decision_id="custom-decision-id")

    assert bundle.decision_id == "custom-decision-id"


@pytest.mark.asyncio
async def test_defaults_decision_id_to_generated_uuid(stack):
    """When omitted, decision_id is auto-generated and non-empty."""
    bundle = await _run(stack)

    assert bundle.decision_id
    assert len(bundle.decision_id) >= 32  # UUID4 string form


@pytest.mark.asyncio
async def test_defaults_idempotency_key_to_event_id(stack):
    """When omitted, idempotency_key falls back to event.event_id."""
    event = _make_event(event_id="evt-fallback-key")
    bundle = await _run(stack, event=event)

    assert bundle.idempotency_key == "evt-fallback-key"


@pytest.mark.asyncio
async def test_uses_provided_idempotency_key(stack):
    """An explicit idempotency_key takes precedence over event.event_id."""
    bundle = await _run(stack, idempotency_key="explicit-key")

    assert bundle.idempotency_key == "explicit-key"


@pytest.mark.asyncio
async def test_populates_latency_breakdown_for_each_phase(stack):
    """Bundle latency_breakdown carries one entry per measurable phase.

    bundle_ms is intentionally not tracked — see app/decide.py for why.
    """
    bundle = await _run(stack)

    expected_phases = {
        "features_ms",
        "scorer_ms",
        "gate_ms",
        "enforcement_ms",
    }
    assert expected_phases.issubset(bundle.latency_breakdown.keys())
    # Fast path: the gate phase is recorded as 0.0 (retrieval now runs
    # inside the policy gate, so it is part of gate_ms when the gate runs).
    assert bundle.latency_breakdown["gate_ms"] == 0.0


@pytest.mark.asyncio
async def test_calls_feature_svc_with_event(stack):
    """feature_svc.compute receives the input event verbatim."""
    event = _make_event(event_id="evt-feature-check")
    await _run(stack, event=event)

    assert stack.feature_svc.compute.call_count == 1
    assert stack.feature_svc.compute.call_args[0][0] is event


@pytest.mark.asyncio
async def test_calls_scorer_with_features(stack):
    """scorer.score receives the AtoFeatureVector from feature_svc.compute."""
    features = stack.feature_svc.compute.return_value
    await _run(stack)

    assert stack.scorer.score.call_count == 1
    assert stack.scorer.score.call_args[0][0] is features


@pytest.mark.asyncio
async def test_the_gate_context_carries_the_policy_gates_retrieval_inputs(stack):
    """The assembler renders the retrieval query and corpus version into the
    policy gate's gate_config — the gate runs retrieval itself (DR-28)."""
    bundle = await _run(stack)

    gate_config = bundle.raw_event.gate_context.gate_config
    assert gate_config is not None
    assert gate_config["corpus_version"] == "corpus-test"
    assert isinstance(gate_config["retrieval_query"], str)
    assert gate_config["retrieval_query"]
