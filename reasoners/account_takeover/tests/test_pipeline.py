"""Tests for the ATO domain pipeline orchestrator.

The ATO route handler and the eval harness's PipelineDriver both delegate
to ``run_ato_decision()``, which scores a login and submits it to the
ledger through a ``LedgerClient`` (DR-31). These tests lock in the
orchestration contract: which services are called with which arguments,
what ATO submits, and what it returns (the ledger's receipt and ATO's own
step timings). The ledger's half — intake, gates, enforcement, the bundle —
is the framework's and is tested there.

Two clients: a recording fake (what ATO hands the ledger), and a
``LocalClient`` over framework services with a gate double (the in-process
path end to end). The gated path through the real policy gate is covered by
the scenario smoke test against the real stack.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.api.models import DecisionReceipt
from app.client import LocalClient
from app.gates import ComposedGate, GateKind
from app.plugin import FrameworkServices
from app.reasoner_registry import LiveReasonerRegistry
from ato_reasoner.events import (
    AuthMethod,
    AuthOutcome,
    Geolocation,
    LoginEvent,
)
from ato_reasoner.features import AtoFeatureVector, WindowSpec
from ato_reasoner.pipeline import AtoDecision, run_ato_decision
from ato_reasoner.plugin import AtoServices
from ato_reasoner.registry import ATO_REGISTRATION
from ato_reasoner.scorer import ScorerOutput
from core.actions import DecisionAction

if TYPE_CHECKING:
    from core.submission import Submission


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


def _make_scorer_output(risk_score: float = 0.05) -> ScorerOutput:
    """A score; 0.05 is in the allow band, so ATO records ALLOW."""
    return ScorerOutput(
        entity_id=uuid4(),
        risk_score=risk_score,
        top_signals=[],
        scorer_version="xgb-test",
        inference_latency_ms=1.0,
    )


class _PolicyGatePlugin:
    """Plugin metadata for the composed policy gate double."""

    gate_id = "policy"
    kind = GateKind.LLM
    deterministic = False
    config_model = None

    def build(self, framework: FrameworkServices) -> MagicMock:
        raise AssertionError("the stack composes its gate directly")


class _RecordingClient:
    """A ledger client that records what ATO submits and answers a receipt."""

    def __init__(self) -> None:
        self.submitted: list[Submission] = []

    async def submit(self, submission: Submission) -> DecisionReceipt:
        self.submitted.append(submission)
        return DecisionReceipt(
            decision_id=str(uuid4()),
            reasoner_id=submission.reasoner_id,
            event_id=submission.event_id,
            entity_id=str(submission.entity_id),
            mode=submission.mode,
            decision_action=submission.proposed_action or DecisionAction.HOLD,
            decision_level_rule=None,
            enforcement_rule_applied=None,
            bypass_rule_id=submission.bypass.rule_id if submission.bypass else None,
            bypass_rule_holds=True if submission.bypass else None,
            registration_version="v",
            gates=[],
            latency_breakdown={"gate_ms": 0.0, "enforcement_ms": 0.1},
            created_at=datetime.now(UTC),
        )


class _Stack:
    """ATO's services on stubs, and a ledger over framework services.

    feature_svc and scorer return real domain objects so build_submission
    can run against them. The composed policy gate and the store are
    MagicMocks (no gate call expected on the recorded path).
    """

    def __init__(self) -> None:
        self.feature_svc = MagicMock()
        self.feature_svc.compute.return_value = _make_features()
        self.scorer = MagicMock()
        self.scorer.score.return_value = _make_scorer_output()
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
            registry=LiveReasonerRegistry([ATO_REGISTRATION]),
            gates={"policy": ComposedGate(plugin=_PolicyGatePlugin(), gate=self.gate)},
        )
        self.recording = _RecordingClient()

    @property
    def written(self):
        [call] = self.store.write.call_args_list
        return call.args[0]


@pytest.fixture
def stack() -> _Stack:
    return _Stack()


async def _local(stack: _Stack, event: LoginEvent | None = None) -> AtoDecision:
    """Run the pipeline against the ledger in process."""
    return await run_ato_decision(
        event=event or _make_event(),
        services=stack.services,
        client=LocalClient(stack.framework),
    )


async def _recorded(stack: _Stack, event: LoginEvent | None = None) -> Submission:
    """Run the pipeline against the recording client; return the submission."""
    await run_ato_decision(
        event=event or _make_event(),
        services=stack.services,
        client=stack.recording,  # type: ignore[arg-type]
    )
    [submission] = stack.recording.submitted
    return submission


# ---------------------------------------------------------------------------
# Through the ledger, in process
# ---------------------------------------------------------------------------


async def test_an_allow_band_score_is_recorded_without_invoking_the_gate(stack):
    """A score in the allow band → a record; the advisory gate is skipped."""
    decision = await _local(stack)

    receipt = decision.receipt
    assert stack.gate.evaluate.call_count == 0
    assert receipt.mode == "record"
    assert receipt.bypass_rule_id == "allow-band"
    assert receipt.bypass_rule_holds is True
    assert receipt.decision_action == DecisionAction.ALLOW
    [policy] = receipt.gates
    assert (policy.gate_id, policy.status) == ("policy", "skipped")
    assert stack.written.decision_id == receipt.decision_id


async def test_a_block_band_score_is_recorded_as_block(stack):
    stack.scorer.score.return_value = _make_scorer_output(0.97)
    receipt = (await _local(stack)).receipt

    assert stack.gate.evaluate.call_count == 0
    assert receipt.bypass_rule_id == "block-band"
    assert receipt.decision_action == DecisionAction.BLOCK
    assert receipt.bypass_rule_holds is True


async def test_a_mid_band_score_asks_the_policy_gate(stack):
    """Between the bands → gate mode; the composed policy gate is invoked.

    The gate double returns something that is not a ``GateOutput``, so the
    framework fails it closed and holds the login (DR-29).
    """
    stack.scorer.score.return_value = _make_scorer_output(0.61)
    receipt = (await _local(stack)).receipt

    assert receipt.mode == "gate"
    assert stack.gate.evaluate.call_count == 1
    [policy] = receipt.gates
    assert policy.status == "failed"
    assert receipt.decision_action == DecisionAction.HOLD


async def test_the_written_bundle_records_atos_registration_version(stack):
    from core.observation import registration_version

    decision = await _local(stack)
    assert decision.receipt.registration_version == registration_version(
        ATO_REGISTRATION
    )
    assert stack.written.registration_version == registration_version(ATO_REGISTRATION)


# ---------------------------------------------------------------------------
# What ATO submits, and what it returns
# ---------------------------------------------------------------------------


async def test_the_submission_is_atos_scored_login(stack):
    event = _make_event(event_id="evt-submitted")
    submission = await _recorded(stack, event)

    assert submission.reasoner_id == ATO_REGISTRATION.reasoner_id
    assert submission.event_id == "evt-submitted"
    assert submission.entity_id == event.entity_id
    assert submission.raw_event == event.model_dump(mode="json")
    assert submission.reasoner_context.label_value == 0.05


async def test_atos_own_steps_are_timed(stack):
    decision = await run_ato_decision(
        event=_make_event(),
        services=stack.services,
        client=stack.recording,  # type: ignore[arg-type]
    )

    assert set(decision.latency_ms) == {"features_ms", "scorer_ms", "ledger_ms"}
    assert decision.total_ms == round(sum(decision.latency_ms.values()), 1)


async def test_calls_feature_svc_with_event(stack):
    """feature_svc.compute receives the input event verbatim."""
    event = _make_event(event_id="evt-feature-check")
    await _recorded(stack, event)

    assert stack.feature_svc.compute.call_count == 1
    assert stack.feature_svc.compute.call_args[0][0] is event


async def test_calls_scorer_with_features(stack):
    """scorer.score receives the AtoFeatureVector from feature_svc.compute."""
    features = stack.feature_svc.compute.return_value
    await _recorded(stack)

    assert stack.scorer.score.call_count == 1
    assert stack.scorer.score.call_args[0][0] is features


async def test_the_gate_context_carries_the_policy_gates_retrieval_inputs(stack):
    """The assembler renders the retrieval query and corpus version into the
    policy gate's gate_config — the gate runs retrieval itself (DR-28)."""
    submission = await _recorded(stack)

    gate_context = submission.gate_context("policy")
    assert gate_context is not None
    gate_config = gate_context.gate_config
    assert gate_config is not None
    assert gate_config["corpus_version"] == "corpus-test"
    assert isinstance(gate_config["retrieval_query"], str)


async def test_a_ledger_refusal_propagates(stack):
    from core.exceptions import LedgerError

    class _Refusing:
        async def submit(self, submission):
            raise LedgerError(403, "REASONER_MISMATCH", "wrong key")

    with pytest.raises(LedgerError) as err:
        await run_ato_decision(
            event=_make_event(),
            services=stack.services,
            client=_Refusing(),  # type: ignore[arg-type]
        )
    assert err.value.code == "REASONER_MISMATCH"
