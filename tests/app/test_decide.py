"""Behavioral tests for ``app.decide.execute_decision`` — the framework half.

``execute_decision`` is the single handoff point between any reasoner and
the framework, and the place gates are dispatched (DR-28). These tests pin
its intake contract, its dispatch, and its gate-output checks without
touching infrastructure or an LLM, using the stub reasoner
(``tests/_stub_reasoner``) and the scripted fake gates (``tests/_fake_gate``):

- Intake validation runs first, against the registry, the named gate's
  ``config_model``, and — when the built gate is a ``ConfigAuthorizer`` —
  that gate's per-reasoner authorization: an unregistered reasoner, a
  disallowed gate, a malformed gate configuration, a configuration the gate
  refuses for this reasoner, or a route to a gate this deployment does not
  compose is rejected before any gate runs, and nothing is persisted.
- On the fast path no gate is called and the bundle is still persisted.
- On the gate route the framework dispatches by ``gate_context.gate_id``,
  hands the gate a ``GateRequest``, and stores the universal ``GateOutput``
  it returns — opaque extension included. A gate that returns anything
  else, names another gate, or misstates its ``deterministic`` flag is
  rejected with ``GatePluginError``; a gate that raises propagates (fail
  closed arrives with the gate set and combinator, DR-29).
- The registration from the registry drives enforcement, and a
  deterministic gate's output skips Tiers 2 and 4.

Fakes are hand-written (no MagicMock on the decision path) so a call on a
path that must not reach a collaborator fails loudly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import Field

from app.decide import DEFAULT_GATE_BUDGET_MS, execute_decision
from app.gates import ComposedGate, ConfigAuthorizer
from app.reasoner_registry import StaticReasonerRegistry
from core.actions import DecisionAction
from core.exceptions import (
    GatePluginError,
    ObservationContractError,
    UnregisteredReasonerError,
)
from core.gate import EvidenceRef, GateInput, GateOutput, GateVerdict
from core.observation import GateContext
from core.routes import GateRoute
from tests._fake_gate.plugin import (
    AuthorizingFakeGate,
    FakeGate,
    FakeGateConfig,
    FakeGateError,
    FakeGatePlugin,
)

if TYPE_CHECKING:
    from core.bundle import DecisionBundle
    from core.observation import ReasonerRegistration

# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------


class _RecordingStore:
    """Bundle store fake that records every ``write`` call."""

    def __init__(self) -> None:
        self.written: list[DecisionBundle] = []

    def write(self, bundle: DecisionBundle) -> None:
        self.written.append(bundle)


class _SubclassedOutput(GateOutput):
    """A gate-private subclass the framework must refuse to store."""

    secret: str = "lost on the store round trip"


class _RichVerdict(GateVerdict):
    """A verdict subclass whose extra field the store would drop."""

    trace: list[str] = Field(default_factory=list)


class _RichEvidence(EvidenceRef):
    """An evidence subclass whose extra field the store would drop."""

    excerpt: str = "lost on the store round trip"


def _rich_verdict_output() -> GateOutput:
    return GateOutput(
        gate_id="fake",
        deterministic=False,
        verdict=_RichVerdict(
            gate_id="fake",
            permitted_actions=[DecisionAction.ALLOW],
            required_controls=[],
            confidence=0.9,
            escalate_to_human=False,
            escalation_reason=None,
            trace=["rule-7 matched"],
        ),
    )


def _rich_evidence_output() -> GateOutput:
    return GateOutput(
        gate_id="fake",
        deterministic=False,
        verdict=None,
        evidence=(_RichEvidence(source_id="doc"),),
    )


_INGESTION_TS = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)


def _compose(*plugins: FakeGatePlugin) -> dict[str, ComposedGate]:
    """Build each plugin's gate the way the bootstrap does."""
    return {
        p.gate_id: ComposedGate(plugin=p, gate=p.build(framework=None))  # type: ignore[arg-type]
        for p in plugins
    }


def _gates() -> dict[str, ComposedGate]:
    return _compose(
        FakeGatePlugin("fake"),
        FakeGatePlugin("fake-rules", deterministic=True),
    )


def _fake(gates: dict[str, ComposedGate], gate_id: str = "fake") -> FakeGate:
    gate = gates[gate_id].gate
    assert isinstance(gate, FakeGate)
    return gate


def _registry_with(registration: ReasonerRegistration) -> StaticReasonerRegistry:
    return StaticReasonerRegistry([registration])


def _routed(observation: Any, gate_id: str = "fake", **config: Any) -> Any:
    """The fixture observation routed to ``gate_id`` with ``config``."""
    return observation.model_copy(
        update={
            "route": GateRoute.ROUTE_TO_GATE,
            "fast_path_rationale": None,
            "gate_context": GateContext(gate_id=gate_id, gate_config=config),
        }
    )


async def _run(
    observation: Any,
    *,
    registry: Any,
    store: _RecordingStore,
    gates: dict[str, ComposedGate] | None = None,
    **kwargs: Any,
) -> DecisionBundle:
    return await execute_decision(
        observation=observation,
        gates=gates if gates is not None else {},
        store=store,  # type: ignore[arg-type]
        registry=registry,
        ingestion_timestamp=_INGESTION_TS,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Intake validation — runs before any gate is touched
# ---------------------------------------------------------------------------


async def test_execute_decision_rejects_unregistered_reasoner(
    observation, stub_registration
):
    store = _RecordingStore()
    gates = _gates()
    registry = _registry_with(
        stub_registration.model_copy(update={"reasoner_id": "other-reasoner"})
    )

    with pytest.raises(UnregisteredReasonerError) as exc_info:
        await _run(_routed(observation), registry=registry, store=store, gates=gates)

    assert exc_info.value.reasoner_id == "stub-reasoner"
    assert store.written == []
    assert _fake(gates).requests == []


def _authorizing_gates(*refused: str) -> dict[str, ComposedGate]:
    return _compose(FakeGatePlugin("fake", refused_reasoners=frozenset(refused)))


async def test_execute_decision_rejects_config_the_gate_refuses_for_the_reasoner(
    observation, stub_registry
):
    # Which configuration a reasoner may send is the gate's call (DR-28):
    # the framework asks the built gate and rejects on refusal.
    store = _RecordingStore()
    gates = _authorizing_gates("stub-reasoner")

    with pytest.raises(ObservationContractError) as exc_info:
        await _run(
            _routed(observation), registry=stub_registry, store=store, gates=gates
        )

    message = str(exc_info.value)
    assert "gate_config for gate 'fake' is not authorized for this reasoner" in message
    assert "may not use fake gate 'fake'" in message
    assert store.written == []
    assert _fake(gates).requests == []


async def test_gate_authorization_runs_on_the_fast_path_too(observation, stub_registry):
    # A recorded decision may not carry a gate context its gate would refuse.
    store = _RecordingStore()
    obs = observation.model_copy(
        update={"gate_context": GateContext(gate_id="fake", gate_config={})}
    )

    with pytest.raises(ObservationContractError, match="not authorized"):
        await _run(
            obs,
            registry=stub_registry,
            store=store,
            gates=_authorizing_gates("stub-reasoner"),
        )

    assert store.written == []


async def test_the_gate_authorizes_the_validated_config_for_the_submitting_reasoner(
    observation, stub_registry
):
    gates = _authorizing_gates("some-other-reasoner")

    bundle = await _run(
        _routed(observation, permitted_actions=["CHALLENGE"]),
        registry=stub_registry,
        store=_RecordingStore(),
        gates=gates,
    )

    gate = _fake(gates)
    assert isinstance(gate, AuthorizingFakeGate)
    ((config, reasoner_id),) = gate.authorizations
    assert reasoner_id == "stub-reasoner"
    # The gate is asked about its config_model instance, not the raw dict.
    assert isinstance(config, FakeGateConfig)
    assert config.permitted_actions == (DecisionAction.CHALLENGE,)
    assert bundle.decision_action == DecisionAction.CHALLENGE


async def test_a_gate_without_a_config_model_is_asked_about_the_raw_config(
    observation, stub_registry
):
    plugin = FakeGatePlugin("fake", refused_reasoners=frozenset())
    plugin.config_model = None
    gates = _compose(plugin)

    await _run(
        _routed(observation, permitted_actions=["HOLD"]),
        registry=stub_registry,
        store=_RecordingStore(),
        gates=gates,
    )

    gate = _fake(gates)
    assert isinstance(gate, AuthorizingFakeGate)
    assert gate.authorizations == [({"permitted_actions": ["HOLD"]}, "stub-reasoner")]


def test_only_a_gate_with_authorize_config_is_a_config_authorizer():
    (plain,) = _compose(FakeGatePlugin("fake")).values()
    (authorizing,) = _authorizing_gates().values()

    assert not isinstance(plain.gate, ConfigAuthorizer)
    assert isinstance(authorizing.gate, ConfigAuthorizer)


async def test_execute_decision_rejects_disallowed_gate_id(
    observation, stub_registration
):
    store = _RecordingStore()
    registry = _registry_with(
        stub_registration.model_copy(
            update={"allowed_gate_ids": frozenset({"some-other-gate"})}
        )
    )

    with pytest.raises(ObservationContractError) as exc_info:
        await _run(observation, registry=registry, store=store)

    assert "gate_id 'policy'" in str(exc_info.value)
    assert store.written == []


async def test_execute_decision_rejects_fast_path_without_rationale(
    observation, stub_registry
):
    # Shape contract (independent of the registry): a fast-path observation
    # must document why the gate was bypassed.
    store = _RecordingStore()
    obs = observation.model_copy(update={"fast_path_rationale": None})

    with pytest.raises(ObservationContractError, match="fast_path_rationale"):
        await _run(obs, registry=stub_registry, store=store)

    assert store.written == []


async def test_execute_decision_rejects_gate_config_that_fails_the_config_model(
    observation, stub_registry
):
    store = _RecordingStore()
    gates = _gates()

    with pytest.raises(ObservationContractError) as exc_info:
        await _run(
            _routed(observation, confidence=7.5),
            registry=stub_registry,
            store=store,
            gates=gates,
        )

    message = str(exc_info.value)
    assert "gate_config for gate 'fake'" in message
    assert "FakeGateConfig" in message
    assert "confidence" in message
    assert store.written == []
    assert _fake(gates).requests == []


async def test_gate_config_is_validated_on_the_fast_path_too(
    observation, stub_registry
):
    # A recorded decision must carry a gate context its gate could evaluate,
    # so shadow evaluation can replay it through the gate later.
    store = _RecordingStore()
    obs = observation.model_copy(
        update={"gate_context": GateContext(gate_id="fake", gate_config={"x": 1})}
    )

    with pytest.raises(ObservationContractError, match="gate 'fake'"):
        await _run(obs, registry=stub_registry, store=store, gates=_gates())

    assert store.written == []


async def test_routing_to_a_gate_the_deployment_does_not_compose_is_rejected(
    observation, stub_registry
):
    store = _RecordingStore()

    with pytest.raises(GatePluginError) as exc_info:
        await _run(
            _routed(observation, gate_id="fake"),
            registry=stub_registry,
            store=store,
            gates=_compose(FakeGatePlugin("fake-rules", deterministic=True)),
        )

    assert exc_info.value.gate_id == "fake"
    assert "does not compose" in str(exc_info.value)
    assert store.written == []


# ---------------------------------------------------------------------------
# Fast path — no gate runs
# ---------------------------------------------------------------------------


async def test_fast_path_persists_bundle_without_calling_any_gate(
    observation, stub_registry
):
    store = _RecordingStore()
    gates = _gates()
    obs = observation.model_copy(
        update={"gate_context": GateContext(gate_id="fake", gate_config={})}
    )

    bundle = await _run(obs, registry=stub_registry, store=store, gates=gates)

    assert store.written == [bundle]
    assert bundle.decision_action == DecisionAction.ALLOW
    assert bundle.gate_input is None
    assert bundle.gate_output is None
    assert _fake(gates).requests == []
    assert _fake(gates, "fake-rules").requests == []


async def test_fast_path_does_not_need_the_named_gate_to_be_composed(
    observation, stub_registry
):
    # The fixture names the policy gate; no gate is composed at all.
    store = _RecordingStore()

    bundle = await _run(observation, registry=stub_registry, store=store, gates={})

    assert bundle.decision_action == DecisionAction.ALLOW
    assert store.written == [bundle]


async def test_fast_path_block_persists_block_action(observation, stub_registry):
    store = _RecordingStore()
    obs = observation.model_copy(
        update={
            "route": GateRoute.FAST_PATH_BLOCK,
            "fast_path_rationale": "widget_score=0.97 > 0.95 → FAST_PATH_BLOCK",
        }
    )

    bundle = await _run(obs, registry=stub_registry, store=store)

    assert bundle.decision_action == DecisionAction.BLOCK
    assert store.written == [bundle]


async def test_fast_path_bundle_carries_identity_and_latency(
    observation, stub_registry
):
    store = _RecordingStore()

    bundle = await _run(
        observation,
        registry=stub_registry,
        store=store,
        upstream_latency={"features_ms": 1.5, "scorer_ms": 2.5},
        decision_id="dec-explicit",
        idempotency_key="idem-explicit",
    )

    assert bundle.decision_id == "dec-explicit"
    assert bundle.idempotency_key == "idem-explicit"
    assert bundle.ingestion_timestamp == _INGESTION_TS
    assert bundle.latency_breakdown["features_ms"] == 1.5
    assert bundle.latency_breakdown["scorer_ms"] == 2.5
    assert bundle.latency_breakdown["gate_ms"] == 0.0
    assert "enforcement_ms" in bundle.latency_breakdown
    # Retrieval is a gate's own concern now (DR-28); the framework measures
    # the gate call as a whole.
    assert "retrieval_ms" not in bundle.latency_breakdown


async def test_idempotency_key_defaults_to_event_id(observation, stub_registry):
    store = _RecordingStore()

    bundle = await _run(observation, registry=stub_registry, store=store)

    assert bundle.idempotency_key == observation.event_id


async def test_fast_path_bundle_keeps_the_stub_event_as_raw_event(
    observation, stub_registry
):
    # The framework stores the reasoner's own Observation type verbatim; it
    # never translates it into a framework-owned event shape.
    store = _RecordingStore()

    bundle = await _run(observation, registry=stub_registry, store=store)

    assert bundle.raw_event is observation
    assert bundle.raw_event.entity_type == "widget"


# ---------------------------------------------------------------------------
# Gate route — dispatch by gate_id, request shape, stored output
# ---------------------------------------------------------------------------


async def test_gate_route_dispatches_to_the_gate_the_context_names(
    observation, stub_registry
):
    store = _RecordingStore()
    gates = _gates()

    await _run(
        _routed(observation, gate_id="fake-rules"),
        registry=stub_registry,
        store=store,
        gates=gates,
    )

    assert len(_fake(gates, "fake-rules").requests) == 1
    assert _fake(gates, "fake").requests == []


async def test_gate_receives_a_request_built_from_the_decision(
    observation, stub_registry
):
    store = _RecordingStore()
    gates = _gates()
    obs = _routed(observation, permitted_actions=["CHALLENGE"])

    bundle = await _run(obs, registry=stub_registry, store=store, gates=gates)

    (request,) = _fake(gates).requests
    assert request.decision_id == bundle.decision_id
    assert request.observation is obs
    assert request.gate_context == obs.gate_context
    assert request.budget_ms == DEFAULT_GATE_BUDGET_MS


async def test_gate_budget_can_be_set_by_the_caller(observation, stub_registry):
    gates = _gates()

    await _run(
        _routed(observation),
        registry=stub_registry,
        store=_RecordingStore(),
        gates=gates,
        gate_budget_ms=250,
    )

    assert _fake(gates).requests[0].budget_ms == 250


async def test_gate_route_stores_the_universal_output_and_records_the_invocation(
    observation, stub_registry
):
    store = _RecordingStore()
    gates = _gates()

    bundle = await _run(
        _routed(observation, permitted_actions=["CHALLENGE"], confidence=0.8),
        registry=stub_registry,
        store=store,
        gates=gates,
    )

    assert store.written == [bundle]
    assert bundle.gate_input == GateInput(gate_id="fake")
    assert type(bundle.gate_output) is GateOutput
    assert bundle.gate_output.gate_id == "fake"
    assert bundle.gate_output.verdict is not None
    assert bundle.gate_output.verdict.permitted_actions == [DecisionAction.CHALLENGE]
    # The gate's opaque extension is stored as the gate wrote it.
    assert bundle.gate_output.extra == {"scripted": True}
    assert bundle.gate_output.evidence_snapshot is not None
    assert bundle.decision_action == DecisionAction.CHALLENGE
    assert bundle.latency_breakdown["gate_ms"] >= 0.0


async def test_gate_route_schema_failure_routes_to_hold_and_persists(
    observation, stub_registry
):
    store = _RecordingStore()
    gates = _gates()

    bundle = await _run(
        _routed(observation, schema_failure=True),
        registry=stub_registry,
        store=store,
        gates=gates,
    )

    assert len(_fake(gates).requests) == 1
    assert bundle.decision_action == DecisionAction.HOLD
    assert bundle.enforcement_rule_applied == "tier1_schema_failure"
    assert bundle.gate_output is not None
    assert bundle.gate_output.verdict is None
    assert store.written == [bundle]


async def test_gate_evidence_jurisdictions_drive_the_conflict_tier(
    observation, stub_registry
):
    # The stub registration's conflict groups are REGION_A vs REGION_B.
    bundle = await _run(
        _routed(observation, evidence_jurisdictions=["REGION_A", "REGION_B"]),
        registry=stub_registry,
        store=_RecordingStore(),
        gates=_gates(),
    )

    assert bundle.decision_action == DecisionAction.HOLD
    assert bundle.enforcement_rule_applied == "tier5_jurisdiction_conflict"


async def test_a_deterministic_gate_skips_the_probe_and_confidence_tiers(
    observation, stub_registry
):
    bundle = await _run(
        _routed(
            observation,
            gate_id="fake-rules",
            permitted_actions=["BLOCK"],
            confidence=0.1,
            required_controls=["adversarial_probe_detected"],
        ),
        registry=stub_registry,
        store=_RecordingStore(),
        gates=_gates(),
    )

    assert bundle.decision_action == DecisionAction.BLOCK
    assert bundle.enforcement_rule_applied is None
    assert bundle.gate_output is not None
    assert bundle.gate_output.deterministic is True
    assert "tier2_adversarial_probe: skipped — deterministic gate" in (
        bundle.override_log
    )
    assert "tier4_low_confidence: skipped — deterministic gate" in (bundle.override_log)


async def test_the_registration_from_the_registry_drives_tier3(
    observation, stub_registry, reasoner_context
):
    rc = reasoner_context.model_copy(
        update={"feature_set": {**reasoner_context.feature_set, "thin_history": True}}
    )
    obs = _routed(observation).model_copy(update={"reasoner_context": rc})

    bundle = await _run(
        obs, registry=stub_registry, store=_RecordingStore(), gates=_gates()
    )

    assert bundle.decision_action == DecisionAction.HOLD
    assert bundle.enforcement_rule_applied == "tier3_novel_entity"


# ---------------------------------------------------------------------------
# Gate output contract — the framework refuses outputs it cannot store
# ---------------------------------------------------------------------------


def _verdictless(gate_id: str, *, deterministic: bool = False) -> GateOutput:
    return GateOutput(gate_id=gate_id, deterministic=deterministic, verdict=None)


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        (_verdictless("fake-rules"), "for gate 'fake-rules'"),
        (_verdictless("fake", deterministic=True), "deterministic=True"),
        (
            _SubclassedOutput(gate_id="fake", deterministic=False, verdict=None),
            "_SubclassedOutput",
        ),
        ({"gate_id": "fake", "verdict": None}, "dict"),
        (_rich_verdict_output(), "_RichVerdict verdict"),
        (_rich_evidence_output(), "_RichEvidence"),
    ],
    ids=[
        "names-another-gate",
        "misstates-determinism",
        "subclass",
        "not-a-gate-output",
        "verdict-subclass",
        "evidence-subclass",
    ],
)
async def test_a_gate_output_the_framework_cannot_trust_is_rejected(
    observation, stub_registry, override, fragment
):
    store = _RecordingStore()
    gates = _gates()
    _fake(gates).output_override = override

    with pytest.raises(GatePluginError) as exc_info:
        await _run(
            _routed(observation), registry=stub_registry, store=store, gates=gates
        )

    assert exc_info.value.gate_id == "fake"
    assert fragment in str(exc_info.value)
    assert store.written == []


async def test_a_gate_that_raises_propagates_and_persists_nothing(
    observation, stub_registry
):
    store = _RecordingStore()

    with pytest.raises(FakeGateError):
        await _run(
            _routed(observation, raise_error=True),
            registry=stub_registry,
            store=store,
            gates=_gates(),
        )

    assert store.written == []


# ---------------------------------------------------------------------------
# A composed gate without a config model (validates on consumption)
# ---------------------------------------------------------------------------


def _unvalidated_gates() -> dict[str, ComposedGate]:
    plugin = FakeGatePlugin("fake")
    plugin.config_model = None
    return _compose(plugin)


async def test_a_gate_without_a_config_model_skips_intake_config_validation(
    observation, stub_registry
):
    # The fast path still records the (unvalidated) context; nothing runs.
    obs = observation.model_copy(
        update={"gate_context": GateContext(gate_id="fake", gate_config={"x": 1})}
    )
    store = _RecordingStore()

    bundle = await _run(
        obs, registry=stub_registry, store=store, gates=_unvalidated_gates()
    )

    assert bundle.decision_action == DecisionAction.ALLOW
    assert store.written == [bundle]


async def test_a_gate_without_a_config_model_receives_its_config_verbatim(
    observation, stub_registry
):
    gates = _unvalidated_gates()

    bundle = await _run(
        _routed(observation, permitted_actions=["HOLD"]),
        registry=stub_registry,
        store=_RecordingStore(),
        gates=gates,
    )

    (request,) = _fake(gates).requests
    assert request.gate_context.gate_config == {"permitted_actions": ["HOLD"]}
    assert bundle.decision_action == DecisionAction.HOLD
