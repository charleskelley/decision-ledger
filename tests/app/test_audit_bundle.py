"""Tests for app/audit/bundle.py — pure DecisionBundle assembly.

Tests are pure (no I/O) and verify that build_bundle() correctly maps all
pipeline layer outputs into the DecisionBundle fields. Tests cover the
fast-path (no gate invocation) and gate-path cases, the gate-layer pairing
invariant, and field defaults. The gate layer is the universal open contract
(DR-28): the bundle stores the ``GateOutput`` a gate returned, opaque
extension included, and has no retrieval layer of its own.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.audit.bundle import build_bundle
from app.enforcement.resolver import resolve
from core.actions import DecisionAction
from core.gate import GateInput, GateOutput
from core.routes import GateRoute
from tests._stub_reasoner.registry import STUB_REGISTRATION

_DECISION_ID = "dec-audit-001"
_IDEMPOTENCY_KEY = "sha256-abc123"
_INGESTION_TS = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)

# Enforcement profile for resolve(): the stub reasoner's registration, which
# declares a novel-entity feature and conflict groups so every tier is live.
_REG = STUB_REGISTRATION


def _enforce(obs, gate_output=None):
    return resolve(obs, gate_output, decision_id=_DECISION_ID, registration=_REG)


def _bundle(obs, **kwargs):
    return build_bundle(
        decision_id=_DECISION_ID,
        obs=obs,
        idempotency_key=_IDEMPOTENCY_KEY,
        ingestion_timestamp=_INGESTION_TS,
        **kwargs,
    )


@pytest.fixture
def gate_route_event(observation):
    """Stub observation routed to the policy gate (ROUTE_TO_GATE)."""
    return observation.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )


# ---------------------------------------------------------------------------
# Fast-path bundle assembly
# ---------------------------------------------------------------------------


def test_build_bundle_fast_path_gate_artifacts_are_none(observation):
    bundle = _bundle(observation, enforcement_decision=_enforce(observation))
    # Fast path — gate never invoked: gate_input and gate_output are both None.
    assert bundle.gate_input is None
    assert bundle.gate_output is None


def test_build_bundle_fast_path_identity_fields(observation):
    bundle = _bundle(
        observation, queue_position=42, enforcement_decision=_enforce(observation)
    )
    assert bundle.decision_id == _DECISION_ID
    assert bundle.idempotency_key == _IDEMPOTENCY_KEY
    assert bundle.ingestion_timestamp == _INGESTION_TS
    assert bundle.queue_position == 42
    assert bundle.raw_event is observation


def test_build_bundle_fast_path_decision_action_is_allow(observation):
    # observation route=FAST_PATH_ALLOW → enforcement returns ALLOW
    bundle = _bundle(observation, enforcement_decision=_enforce(observation))
    assert bundle.decision_action == DecisionAction.ALLOW
    assert bundle.enforcement_rule_applied is None


def test_build_bundle_created_at_is_utc_and_recent(observation):
    before = datetime.now(UTC)
    bundle = _bundle(observation, enforcement_decision=_enforce(observation))
    after = datetime.now(UTC)
    assert bundle.created_at.tzinfo is not None
    assert before <= bundle.created_at <= after


# ---------------------------------------------------------------------------
# Gate-path bundle assembly
# ---------------------------------------------------------------------------


def test_build_bundle_gate_path_stores_the_gate_output_as_returned(
    gate_route_event, gate_output
):
    gate_input = GateInput(gate_id="policy")
    bundle = _bundle(
        gate_route_event,
        gate_input=gate_input,
        gate_output=gate_output,
        enforcement_decision=_enforce(gate_route_event, gate_output),
    )
    assert bundle.gate_input is gate_input
    assert bundle.gate_output is gate_output
    assert bundle.gate_output.evidence_snapshot == {
        "retrieval_query": "calibration review"
    }
    assert bundle.gate_output.extra == {
        "rationale": "Low widget score. History sufficient."
    }
    assert bundle.decision_action == DecisionAction.ALLOW


def test_build_bundle_schema_failure_routes_to_hold(gate_route_event):
    """Gate verdict None → enforcement tier1_schema_failure → HOLD."""
    failed = GateOutput(
        gate_id="policy",
        deterministic=False,
        verdict=None,
        extra={"failure": "llm_error"},
    )
    bundle = _bundle(
        gate_route_event,
        gate_input=GateInput(gate_id="policy"),
        gate_output=failed,
        enforcement_decision=_enforce(gate_route_event, failed),
    )
    assert bundle.decision_action == DecisionAction.HOLD
    assert bundle.enforcement_rule_applied == "tier1_schema_failure"
    # The no-verdict invariant: gate_output is non-None but verdict is None;
    # the gate's extra carries the forensic record for HOLD review.
    assert bundle.gate_output is not None
    assert bundle.gate_output.verdict is None
    assert bundle.gate_output.extra == {"failure": "llm_error"}


@pytest.mark.parametrize("missing", ["gate_input", "gate_output"])
def test_build_bundle_requires_gate_input_and_output_together(
    gate_route_event, gate_output, missing
):
    layers = {"gate_input": GateInput(gate_id="policy"), "gate_output": gate_output}
    del layers[missing]
    with pytest.raises(ValueError, match="both"):
        _bundle(
            gate_route_event,
            enforcement_decision=_enforce(gate_route_event, gate_output),
            **layers,
        )


def test_build_bundle_latency_breakdown_stored(observation):
    latency = {"ingestion_ms": 5.2, "features_ms": 3.1, "scorer_ms": 1.8}
    bundle = _bundle(
        observation,
        enforcement_decision=_enforce(observation),
        latency_breakdown=latency,
    )
    assert bundle.latency_breakdown["ingestion_ms"] == pytest.approx(5.2)
    assert bundle.latency_breakdown["scorer_ms"] == pytest.approx(1.8)


def test_build_bundle_override_log_from_enforcement(observation):
    bundle = _bundle(observation, enforcement_decision=_enforce(observation))
    # Fast path — single log entry
    assert len(bundle.override_log) == 1
    assert "fast_path_allow" in bundle.override_log[0]
