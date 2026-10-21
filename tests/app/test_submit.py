"""Behavioral tests for ``app.submit.submit`` — the framework half of a decision.

``submit`` is the single handoff point between any reasoner and the ledger
(DR-29 to DR-31). These tests pin, without infrastructure or an LLM, using
the stub reasoner (``tests/_stub_reasoner``) and the scripted fake gates
(``tests/_fake_gate``):

- **Intake**: an unregistered reasoner, a citation the registration does not
  back, a gate context outside the gate set, a missing context, a context
  its gate's ``config_model`` rejects, or one the gate refuses for this
  reasoner is rejected before any gate runs, and nothing is persisted.
- **Record mode**: only mandatory gates run; advisory gates are recorded as
  skipped; the cited rule is evaluated and snapshotted; a rule that does
  not hold fails closed to HOLD; a mandatory gate (bound, or mandated by the
  deployment) can raise the recorded action.
- **Gate mode**: mandatory then advisory gates, each phase concurrent; the
  advisory phase is skipped when the decision is already BLOCK; the result
  does not depend on gate order.
- **Fail closed**: a gate that raises (a timeout or cancellation of its own
  included), times out, returns an output the framework cannot store
  faithfully, or is not composed is recorded as ``failed`` and holds the
  decision; the bundle is still written. A gate's budget is charged only
  with its own evaluation, and only the caller's cancellation propagates.
- **Registration version** (DR-31): every bundle records the version of the
  registration intake used; ``decide`` returns the bundle ``submit`` would
  write, without writing it.
- **Identity** (DR-32): after intake and before any gate, the identity step
  establishes the decision's subject; every gate sees it
  (``GateRequest.identity``) and the bundle records it. A native subject
  that contradicts its declared namespace is refused at intake; a resolver
  that fails, times out, or finds nothing never refuses a decision (the
  subject is recorded unresolved and the bundle is written).

Fakes are hand-written (no MagicMock on the decision path) so a call on a
path that must not reach a collaborator fails loudly.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import Field

from app.gates import ComposedGate, ConfigAuthorizer, invoke_gate
from app.reasoner_registry import LiveReasonerRegistry
from app.submit import (
    DEFAULT_GATE_BUDGET_MS,
    SKIPPED_FOR_BLOCK,
    SKIPPED_FOR_RECORD,
    decide,
    submit,
)
from core.actions import DecisionAction
from core.exceptions import SubmissionContractError, UnregisteredReasonerError
from core.gate import EvidenceRef, GateInput, GateOutput, GateVerdict
from core.gate_request import GateRequest
from core.observation import GateBinding, GateContext, registration_version
from core.submission import BypassCitation, storage_problem
from tests._fake_gate.plugin import (
    AuthorizingFakeGate,
    FakeGate,
    FakeGateConfig,
    FakeGatePlugin,
    FakeLookupGatePlugin,
)
from tests._stub_reasoner.registry import LOW_SCORE_RULE, STUB_REGISTRATION

if TYPE_CHECKING:
    from core.bundle import DecisionBundle
    from core.observation import ReasonerRegistration
    from core.submission import Submission

ALLOW, CHALLENGE, HOLD, BLOCK = (
    DecisionAction.ALLOW,
    DecisionAction.CHALLENGE,
    DecisionAction.HOLD,
    DecisionAction.BLOCK,
)
_INGESTION_TS = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)

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


def _compose(*plugins: Any, mandatory: tuple[str, ...] = ()) -> dict[str, ComposedGate]:
    """Build each plugin's gate the way the bootstrap does."""
    return {
        p.gate_id: ComposedGate(
            plugin=p,
            gate=p.build(framework=None),  # type: ignore[arg-type]
            mandatory=p.gate_id in mandatory,
        )
        for p in plugins
    }


def _gate(gates: dict[str, ComposedGate], gate_id: str = "fake") -> Any:
    return gates[gate_id].gate


def _registry(registration: ReasonerRegistration = STUB_REGISTRATION):
    return LiveReasonerRegistry([registration])


def _binding(gate_id: str, *, mandatory: bool = False) -> GateBinding:
    return GateBinding(gate_id=gate_id, mandatory=mandatory)


def _revalidated(registration: ReasonerRegistration) -> ReasonerRegistration:
    """Run a variant through the registration validators (model_copy skips them)."""
    return type(registration).model_validate_json(registration.model_dump_json())


def _with_gates(*bindings: GateBinding) -> ReasonerRegistration:
    return _revalidated(STUB_REGISTRATION.model_copy(update={"gates": tuple(bindings)}))


def _contexts(submission: Submission, *contexts: GateContext) -> Submission:
    return submission.model_copy(update={"gate_contexts": tuple(contexts)})


def _scripted(submission: Submission, gate_id: str = "fake", **config: Any):
    """The submission addressing ``gate_id`` with a scripted fake config."""
    return _contexts(submission, GateContext(gate_id=gate_id, gate_config=config))


async def _run(
    submission: Submission,
    *,
    gates: dict[str, ComposedGate],
    registry: Any = None,
    store: _RecordingStore | None = None,
    **kwargs: Any,
) -> DecisionBundle:
    return await submit(
        submission=submission,
        gates=gates,
        store=store if store is not None else _RecordingStore(),  # type: ignore[arg-type]
        registry=registry if registry is not None else _registry(),
        ingestion_timestamp=_INGESTION_TS,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Intake — runs before any gate is touched; nothing is persisted
# ---------------------------------------------------------------------------


async def test_an_unregistered_reasoner_is_rejected(gated_submission):
    store, gates = _RecordingStore(), _compose(FakeGatePlugin())
    other = _registry(
        _revalidated(STUB_REGISTRATION.model_copy(update={"reasoner_id": "other"}))
    )
    with pytest.raises(UnregisteredReasonerError) as exc_info:
        await _run(gated_submission, gates=gates, registry=other, store=store)
    assert exc_info.value.reasoner_id == "stub-reasoner"
    assert store.written == []
    assert _gate(gates).requests == []


async def test_a_citation_the_registration_does_not_back_is_rejected(submission):
    store, gates = _RecordingStore(), _compose(FakeGatePlugin())
    forged = submission.model_copy(update={"bypass": BypassCitation(rule_id="nope")})
    with pytest.raises(SubmissionContractError) as exc_info:
        await _run(forged, gates=gates, store=store)
    assert exc_info.value.code == "BYPASS_RULE_UNREGISTERED"
    assert store.written == []


async def test_a_context_for_an_unbound_gate_is_rejected(gated_submission):
    store, gates = _RecordingStore(), _compose(FakeGatePlugin(), FakeGatePlugin("x"))
    rogue = _contexts(
        gated_submission, GateContext(gate_id="fake"), GateContext(gate_id="x")
    )
    with pytest.raises(SubmissionContractError) as exc_info:
        await _run(rogue, gates=gates, store=store)
    assert exc_info.value.code == "GATE_NOT_BOUND"
    assert store.written == []
    assert _gate(gates, "x").requests == []


async def test_a_missing_context_for_a_bound_gate_is_rejected(submission):
    store = _RecordingStore()
    with pytest.raises(SubmissionContractError) as exc_info:
        await _run(_contexts(submission), gates=_compose(FakeGatePlugin()), store=store)
    assert exc_info.value.code == "GATE_CONTEXT_MISSING"
    assert store.written == []


@pytest.mark.parametrize("fixture", ["submission", "gated_submission"])
async def test_a_gate_config_its_config_model_rejects_is_rejected_in_both_modes(
    request, fixture
):
    store, gates = _RecordingStore(), _compose(FakeGatePlugin())
    base = request.getfixturevalue(fixture)
    with pytest.raises(SubmissionContractError) as exc_info:
        await _run(_scripted(base, confidence=7.0), gates=gates, store=store)
    assert exc_info.value.code == "GATE_CONFIG_INVALID"
    assert "FakeGateConfig" in exc_info.value.violation
    assert store.written == []
    assert _gate(gates).requests == []


async def test_a_config_the_gate_refuses_for_the_reasoner_is_rejected(submission):
    store = _RecordingStore()
    gates = _compose(FakeGatePlugin(refused_reasoners=frozenset({"stub-reasoner"})))
    with pytest.raises(SubmissionContractError) as exc_info:
        # A record: the authorization runs even though the gate will not.
        await _run(submission, gates=gates, store=store)
    assert exc_info.value.code == "GATE_CONFIG_UNAUTHORIZED"
    assert "may not use fake gate" in exc_info.value.violation
    assert store.written == []


async def test_the_gate_authorizes_the_validated_config_for_the_submitting_reasoner(
    gated_submission,
):
    gates = _compose(FakeGatePlugin(refused_reasoners=frozenset()))
    await _run(_scripted(gated_submission, confidence=0.8), gates=gates)
    gate = _gate(gates)
    assert isinstance(gate, AuthorizingFakeGate)
    [(config, reasoner_id)] = gate.authorizations
    assert isinstance(config, FakeGateConfig)
    assert config.confidence == 0.8
    assert reasoner_id == "stub-reasoner"


def test_only_a_gate_with_authorize_config_is_a_config_authorizer():
    plain = FakeGatePlugin().build(framework=None)  # type: ignore[arg-type]
    authorizing = FakeGatePlugin(refused_reasoners=frozenset()).build(framework=None)  # type: ignore[arg-type]
    assert not isinstance(plain, ConfigAuthorizer)
    assert isinstance(authorizing, ConfigAuthorizer)


async def test_a_mandated_gate_that_needs_configuration_explains_the_fix(submission):
    # The deployment mandates a gate whose config model requires a field; a
    # reasoner that does not bind it sends no context, so intake says how to
    # fix it rather than failing the gate at decision time.
    store = _RecordingStore()
    gates = _compose(FakeGatePlugin(), _RequiringPlugin("needy"), mandatory=("needy",))
    with pytest.raises(SubmissionContractError) as exc_info:
        await _run(submission, gates=gates, store=store)
    assert exc_info.value.code == "GATE_CONFIG_INVALID"
    assert "bind it in the registration" in exc_info.value.violation
    assert store.written == []


class _NeedsField(FakeGateConfig):
    ruleset_id: str


class _RequiringPlugin(FakeGatePlugin):
    def __init__(self, gate_id: str) -> None:
        super().__init__(gate_id, deterministic=True)
        self.config_model = _NeedsField


# ---------------------------------------------------------------------------
# Record mode
# ---------------------------------------------------------------------------


async def test_a_record_persists_its_action_without_calling_an_advisory_gate(
    submission,
):
    store, gates = _RecordingStore(), _compose(FakeGatePlugin())
    bundle = await _run(submission, gates=gates, store=store)

    assert store.written == [bundle]
    assert bundle.decision_action == ALLOW
    assert bundle.decision_level_rule is None
    assert bundle.submission == submission
    [record] = bundle.gates
    assert (record.gate_id, record.mandatory, record.status) == (
        "fake",
        False,
        "skipped",
    )
    assert record.reason == SKIPPED_FOR_RECORD
    assert _gate(gates).requests == []
    assert bundle.bypass_rule_holds is True
    assert bundle.bypass_check is not None
    assert bundle.bypass_check.rule == LOW_SCORE_RULE
    assert bundle.bypass_check.observed == {"label_value": 0.15}


async def test_a_record_does_not_need_its_advisory_gate_composed(submission):
    bundle = await _run(submission, gates={})
    assert bundle.decision_action == ALLOW
    assert bundle.gates[0].status == "skipped"


async def test_a_recorded_block_is_persisted_as_block(submission):
    rc = submission.reasoner_context.model_copy(update={"label_value": 0.97})
    block = submission.model_copy(
        update={
            "reasoner_context": rc,
            "proposed_action": BLOCK,
            "bypass": BypassCitation(rule_id="high-score"),
        }
    )
    bundle = await _run(block, gates=_compose(FakeGatePlugin()))
    assert bundle.decision_action == BLOCK
    assert bundle.bypass_rule_holds is True


async def test_a_record_whose_cited_rule_does_not_hold_fails_closed_to_hold(
    submission,
):
    rc = submission.reasoner_context.model_copy(update={"label_value": 0.5})
    unjustified = submission.model_copy(update={"reasoner_context": rc})
    bundle = await _run(unjustified, gates=_compose(FakeGatePlugin()))
    assert bundle.decision_action == HOLD
    assert bundle.decision_level_rule == "bypass_rule_not_satisfied"
    assert bundle.bypass_rule_holds is False


async def test_a_bound_mandatory_lookup_gate_raises_a_recorded_allow(submission):
    lookup = FakeLookupGatePlugin(listed={submission.entity_id: BLOCK})
    registry = _registry(
        _with_gates(_binding("fake"), _binding("fake-lookup", mandatory=True))
    )
    record = _contexts(
        submission, GateContext(gate_id="fake"), GateContext(gate_id="fake-lookup")
    )
    gates = _compose(FakeGatePlugin(), lookup)

    bundle = await _run(record, gates=gates, registry=registry)

    assert bundle.decision_action == BLOCK
    lookup_record, fake_record = bundle.gates
    assert (lookup_record.gate_id, lookup_record.status) == ("fake-lookup", "ran")
    assert lookup_record.mandatory is True
    assert lookup_record.enforced_action == BLOCK
    assert fake_record.status == "skipped"
    assert bundle.override_log[-1] == (
        "combinator: most_restrictive(proposed=ALLOW, mandatory:fake-lookup=BLOCK) "
        "→ BLOCK"
    )


async def test_an_unlisted_subject_passes_the_mandatory_lookup_gate(submission):
    registry = _registry(
        _with_gates(_binding("fake"), _binding("fake-lookup", mandatory=True))
    )
    record = _contexts(
        submission, GateContext(gate_id="fake"), GateContext(gate_id="fake-lookup")
    )
    bundle = await _run(
        record,
        gates=_compose(FakeGatePlugin(), FakeLookupGatePlugin()),
        registry=registry,
    )
    assert bundle.decision_action == ALLOW


async def test_a_deployment_mandated_gate_runs_on_every_record(submission):
    lookup = FakeLookupGatePlugin(listed={submission.entity_id: HOLD})
    gates = _compose(FakeGatePlugin(), lookup, mandatory=("fake-lookup",))

    bundle = await _run(submission, gates=gates)

    assert bundle.decision_action == HOLD
    record = bundle.gate("fake-lookup")
    assert record is not None
    assert record.mandatory is True
    assert record.status == "ran"
    # The stub does not bind it: it ran with an empty configuration.
    [request] = lookup.built[0].requests
    assert request.gate_context == GateContext(gate_id="fake-lookup")


async def test_a_deployment_mandate_makes_a_bound_advisory_gate_mandatory(submission):
    gates = _compose(FakeGatePlugin(), mandatory=("fake",))
    bundle = await _run(_scripted(submission, permitted_actions=["HOLD"]), gates=gates)
    [record] = bundle.gates
    assert record.mandatory is True
    assert record.status == "ran"
    assert bundle.decision_action == HOLD


async def test_a_record_bundle_carries_identity_latency_and_the_raw_event(submission):
    bundle = await _run(
        submission,
        gates=_compose(FakeGatePlugin()),
        upstream_latency={"features_ms": 1.0, "scorer_ms": 2.0},
        decision_id="11111111-1111-1111-1111-111111111111",
    )
    assert bundle.decision_id == "11111111-1111-1111-1111-111111111111"
    assert bundle.ingestion_timestamp == _INGESTION_TS
    assert bundle.idempotency_key == submission.event_id
    assert set(bundle.latency_breakdown) == {
        "features_ms",
        "scorer_ms",
        "identity_ms",
        "gate_ms",
        "enforcement_ms",
    }
    assert bundle.submission.raw_event == submission.raw_event
    assert bundle.submission.raw_event["widget_id"] == "widget-12345"


async def test_the_idempotency_key_can_be_set_by_the_caller(submission):
    bundle = await _run(submission, gates={}, idempotency_key="k-1")
    assert bundle.idempotency_key == "k-1"


# ---------------------------------------------------------------------------
# Gate mode
# ---------------------------------------------------------------------------


async def test_a_gate_request_dispatches_to_its_bound_gate_and_stores_its_output(
    gated_submission,
):
    store, gates = _RecordingStore(), _compose(FakeGatePlugin())
    bundle = await _run(
        _scripted(gated_submission, permitted_actions=["CHALLENGE"]),
        gates=gates,
        store=store,
    )
    assert store.written == [bundle]
    assert bundle.decision_action == CHALLENGE
    [record] = bundle.gates
    assert record.status == "ran"
    assert record.input == GateInput(gate_id="fake")
    assert record.output is not None
    assert record.output.extra == {"scripted": True}
    assert record.output.evidence_snapshot == {
        "config": FakeGateConfig(permitted_actions=(CHALLENGE,)).model_dump(
            mode="json"
        ),
        "request_number": 1,
    }
    assert record.duration_ms is not None
    assert bundle.bypass_check is None
    assert bundle.bypass_rule_holds is None


async def test_the_gate_receives_a_request_built_from_the_decision(gated_submission):
    gates = _compose(FakeGatePlugin())
    scripted = _scripted(gated_submission, confidence=0.9)
    bundle = await _run(
        scripted, gates=gates, decision_id="22222222-2222-2222-2222-222222222222"
    )
    [request] = _gate(gates).requests
    assert request == GateRequest(
        decision_id=bundle.decision_id,
        submission=scripted,
        gate_context=scripted.gate_contexts[0],
        budget_ms=DEFAULT_GATE_BUDGET_MS,
        identity=bundle.identity,
    )
    assert request.identity is not None
    assert request.identity.entity_id == scripted.entity_id


async def test_the_gate_budget_can_be_set_by_the_caller(gated_submission):
    gates = _compose(FakeGatePlugin())
    await _run(gated_submission, gates=gates, gate_budget_ms=1_234)
    assert _gate(gates).requests[0].budget_ms == 1_234


async def test_a_schema_failure_holds_and_persists(gated_submission):
    store = _RecordingStore()
    bundle = await _run(
        _scripted(gated_submission, schema_failure=True),
        gates=_compose(FakeGatePlugin()),
        store=store,
    )
    assert bundle.decision_action == HOLD
    [record] = bundle.gates
    assert record.status == "ran"
    assert record.output is not None
    assert record.output.verdict is None
    assert record.enforcement is not None
    assert record.enforcement.enforcement_rule_applied == "tier1_schema_failure"
    assert store.written == [bundle]


async def test_gate_evidence_jurisdictions_drive_the_conflict_tier(gated_submission):
    bundle = await _run(
        _scripted(gated_submission, evidence_jurisdictions=["REGION_A", "REGION_B"]),
        gates=_compose(FakeGatePlugin()),
    )
    assert bundle.decision_action == HOLD
    assert bundle.gates[0].enforcement.enforcement_rule_applied == (  # type: ignore[union-attr]
        "tier5_jurisdiction_conflict"
    )


async def test_a_deterministic_gate_skips_the_probe_and_confidence_tiers(
    gated_submission,
):
    gates = _compose(FakeGatePlugin("fake", deterministic=True))
    bundle = await _run(
        _scripted(
            gated_submission,
            permitted_actions=["BLOCK"],
            confidence=0.1,
            required_controls=["adversarial_probe_detected"],
        ),
        gates=gates,
    )
    assert bundle.decision_action == BLOCK
    log = bundle.gates[0].enforcement.override_log  # type: ignore[union-attr]
    assert "tier2_adversarial_probe: skipped — deterministic gate" in log
    assert "tier4_low_confidence: skipped — deterministic gate" in log


async def test_the_registration_from_the_registry_drives_tier3(gated_submission):
    rc = gated_submission.reasoner_context
    novel = gated_submission.model_copy(
        update={
            "reasoner_context": rc.model_copy(
                update={"feature_set": {**rc.feature_set, "thin_history": True}}
            )
        }
    )
    gates = _compose(FakeGatePlugin())
    held = await _run(_scripted(novel, permitted_actions=["BLOCK"]), gates=gates)
    assert held.decision_action == HOLD
    assert held.decision_level_rule == "tier3_novel_entity"

    no_tier3 = _registry(
        _revalidated(
            STUB_REGISTRATION.model_copy(update={"novel_entity_feature": None})
        )
    )
    blocked = await _run(
        _scripted(novel, permitted_actions=["BLOCK"]), gates=gates, registry=no_tier3
    )
    assert blocked.decision_action == BLOCK


async def test_two_advisory_gates_both_run_and_combine(gated_submission):
    registry = _registry(_with_gates(_binding("fake"), _binding("other")))
    gates = _compose(FakeGatePlugin(), FakeGatePlugin("other"))
    submission = _contexts(
        gated_submission,
        GateContext(gate_id="fake", gate_config={"permitted_actions": ["ALLOW"]}),
        GateContext(gate_id="other", gate_config={"permitted_actions": ["CHALLENGE"]}),
    )
    bundle = await _run(submission, gates=gates, registry=registry)
    assert bundle.decision_action == CHALLENGE
    assert [(r.gate_id, r.status) for r in bundle.gates] == [
        ("fake", "ran"),
        ("other", "ran"),
    ]


async def test_mandatory_gates_are_recorded_first_then_advisory_ones(gated_submission):
    registry = _registry(
        _with_gates(_binding("fake"), _binding("fake-lookup", mandatory=True))
    )
    submission = _contexts(
        gated_submission,
        GateContext(gate_id="fake"),
        GateContext(gate_id="fake-lookup"),
    )
    bundle = await _run(
        submission,
        gates=_compose(FakeGatePlugin(), FakeLookupGatePlugin()),
        registry=registry,
    )
    assert [(r.gate_id, r.mandatory, r.status) for r in bundle.gates] == [
        ("fake-lookup", True, "ran"),
        ("fake", False, "ran"),
    ]


async def test_a_mandatory_block_skips_the_advisory_gates(gated_submission):
    lookup = FakeLookupGatePlugin(listed={gated_submission.entity_id: BLOCK})
    registry = _registry(
        _with_gates(_binding("fake"), _binding("fake-lookup", mandatory=True))
    )
    submission = _contexts(
        gated_submission,
        GateContext(gate_id="fake"),
        GateContext(gate_id="fake-lookup"),
    )
    gates = _compose(FakeGatePlugin(), lookup)

    bundle = await _run(submission, gates=gates, registry=registry)

    assert bundle.decision_action == BLOCK
    advisory = bundle.gate("fake")
    assert advisory is not None
    assert (advisory.status, advisory.reason) == ("skipped", SKIPPED_FOR_BLOCK)
    assert _gate(gates).requests == []


async def test_a_proposed_block_still_runs_the_advisory_gates(gated_submission):
    # The proposal is a floor, never a reason to skip: otherwise a reasoner
    # could BLOCK without gating and without a bypass rule.
    gates = _compose(FakeGatePlugin())
    proposed = gated_submission.model_copy(update={"proposed_action": BLOCK})
    bundle = await _run(proposed, gates=gates)
    assert bundle.decision_action == BLOCK
    assert bundle.gates[0].status == "ran"
    assert len(_gate(gates).requests) == 1


async def test_a_mandatory_hold_does_not_skip_the_advisory_gates(gated_submission):
    lookup = FakeLookupGatePlugin(listed={gated_submission.entity_id: HOLD})
    registry = _registry(
        _with_gates(_binding("fake"), _binding("fake-lookup", mandatory=True))
    )
    submission = _contexts(
        gated_submission,
        GateContext(gate_id="fake", gate_config={"permitted_actions": ["BLOCK"]}),
        GateContext(gate_id="fake-lookup"),
    )
    bundle = await _run(
        submission, gates=_compose(FakeGatePlugin(), lookup), registry=registry
    )
    assert bundle.decision_action == BLOCK
    assert bundle.gate("fake").status == "ran"  # type: ignore[union-attr]


async def test_the_gates_of_a_phase_run_concurrently(gated_submission):
    registry = _registry(_with_gates(_binding("a"), _binding("b"), _binding("c")))
    gates = _compose(FakeGatePlugin("a"), FakeGatePlugin("b"), FakeGatePlugin("c"))
    submission = _contexts(
        gated_submission,
        *(
            GateContext(gate_id=g, gate_config={"delay_ms": 150})
            for g in ("a", "b", "c")
        ),
    )
    t0 = time.perf_counter()
    bundle = await _run(submission, gates=gates, registry=registry)
    elapsed_ms = (time.perf_counter() - t0) * 1_000
    # Three 150 ms gates in parallel take ~150 ms, not ~450 ms.
    assert elapsed_ms < 400
    assert bundle.latency_breakdown["gate_ms"] < 400
    assert all(r.status == "ran" for r in bundle.gates)


async def test_gate_order_and_finishing_order_cannot_change_the_decision(
    gated_submission,
):
    # Each gate finishes at a different time; every binding order gives the
    # same action, and records follow the binding order, not finishing order.
    scripts = {
        "a": {"permitted_actions": ["ALLOW"], "delay_ms": 30},
        "b": {"permitted_actions": ["CHALLENGE"], "delay_ms": 0},
        "c": {"permitted_actions": ["HOLD"], "delay_ms": 15},
    }
    gates = _compose(*(FakeGatePlugin(g) for g in scripts))
    actions = set()
    for order in itertools.permutations(scripts):
        registry = _registry(_with_gates(*(_binding(g) for g in order)))
        submission = _contexts(
            gated_submission,
            *(GateContext(gate_id=g, gate_config=scripts[g]) for g in order),
        )
        bundle = await _run(submission, gates=gates, registry=registry)
        assert [r.gate_id for r in bundle.gates] == list(order)
        actions.add(bundle.decision_action)
    assert actions == {HOLD}


async def test_a_gate_without_a_config_model_gets_its_config_unvalidated(
    gated_submission,
):
    plugin = FakeGatePlugin()
    plugin.config_model = None
    gates = _compose(plugin)
    raw = {"anything": [1, 2], "permitted_actions": ["ALLOW"]}
    # FakeGate itself would reject the unknown key; the framework does not.
    bundle = await _run(_scripted(gated_submission, **raw), gates=gates)
    [request] = _gate(gates).requests
    assert request.gate_context.gate_config == raw
    # The fake gate fails validating "anything" itself: a gate error.
    assert bundle.gates[0].status == "failed"


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


async def test_a_gate_that_raises_fails_closed_and_the_bundle_is_written(
    gated_submission,
):
    store = _RecordingStore()
    bundle = await _run(
        _scripted(gated_submission, raise_error=True),
        gates=_compose(FakeGatePlugin()),
        store=store,
    )
    assert store.written == [bundle]
    assert bundle.decision_action == HOLD
    [record] = bundle.gates
    assert record.status == "failed"
    assert (
        record.reason == "raised FakeGateError: fake gate 'fake' was scripted to fail"
    )
    assert record.input == GateInput(gate_id="fake")
    assert record.output is None
    assert record.enforcement is not None
    assert record.enforcement.override_log == [
        "tier1_schema_failure: fired — Gate failed (raised FakeGateError: fake gate "
        "'fake' was scripted to fail) — routed to HOLD for review"
    ]


async def test_a_gate_that_outlives_its_budget_is_cancelled_and_fails_closed(
    gated_submission,
):
    bundle = await _run(
        _scripted(gated_submission, delay_ms=2_000),
        gates=_compose(FakeGatePlugin()),
        gate_budget_ms=50,
    )
    assert bundle.decision_action == HOLD
    [record] = bundle.gates
    assert record.status == "failed"
    assert record.reason == "timed out after 50 ms (budget)"
    assert record.duration_ms is not None
    assert record.duration_ms < 1_000


def _rich_verdict_output() -> GateOutput:
    return GateOutput(
        gate_id="fake",
        deterministic=False,
        verdict=_RichVerdict(
            gate_id="fake",
            permitted_actions=[ALLOW],
            required_controls=[],
            confidence=0.9,
            escalate_to_human=False,
            escalation_reason=None,
            trace=["rule-7 matched"],
        ),
    )


@pytest.mark.parametrize(
    ("override", "problem"),
    [
        (
            _SubclassedOutput(gate_id="fake", deterministic=False, verdict=None),
            "evaluate() returned _SubclassedOutput",
        ),
        (_rich_verdict_output(), "the verdict is a _RichVerdict"),
        (
            GateOutput(
                gate_id="fake",
                deterministic=False,
                verdict=None,
                evidence=(_RichEvidence(source_id="doc"),),
            ),
            "evidence of type(s) ['_RichEvidence']",
        ),
        (
            GateOutput(gate_id="other", deterministic=False, verdict=None),
            "a GateOutput for gate 'other'",
        ),
        (
            GateOutput(gate_id="fake", deterministic=True, verdict=None),
            "deterministic=True, but the plugin declares deterministic=False",
        ),
        ({"verdict": "ALLOW"}, "evaluate() returned dict"),
    ],
)
async def test_an_output_the_framework_cannot_store_faithfully_fails_closed(
    gated_submission, override, problem
):
    gates = _compose(FakeGatePlugin())
    _gate(gates).output_override = override
    bundle = await _run(gated_submission, gates=gates)
    assert bundle.decision_action == HOLD
    [record] = bundle.gates
    assert record.status == "failed"
    assert record.reason is not None
    assert record.reason.startswith("returned a malformed output: ")
    assert problem in record.reason
    assert record.output is None


async def test_a_bound_gate_that_is_not_composed_fails_closed_in_gate_mode(
    gated_submission,
):
    bundle = await _run(gated_submission, gates={})
    assert bundle.decision_action == HOLD
    [record] = bundle.gates
    assert record.status == "failed"
    assert record.reason == "no gate 'fake' is composed in this deployment"
    assert record.input is None
    assert record.duration_ms is None


async def test_a_failed_mandatory_gate_holds_a_record(submission):
    registry = _registry(
        _with_gates(_binding("fake"), _binding("fake-lookup", mandatory=True))
    )
    record = _contexts(
        submission, GateContext(gate_id="fake"), GateContext(gate_id="fake-lookup")
    )
    bundle = await _run(record, gates=_compose(FakeGatePlugin()), registry=registry)
    assert bundle.decision_action == HOLD
    assert bundle.gate("fake-lookup").status == "failed"  # type: ignore[union-attr]


async def test_one_failing_gate_does_not_stop_the_others(gated_submission):
    registry = _registry(_with_gates(_binding("fake"), _binding("other")))
    gates = _compose(FakeGatePlugin(), FakeGatePlugin("other"))
    submission = _contexts(
        gated_submission,
        GateContext(gate_id="fake", gate_config={"raise_error": True}),
        GateContext(gate_id="other", gate_config={"permitted_actions": ["CHALLENGE"]}),
    )
    bundle = await _run(submission, gates=gates, registry=registry)
    assert [r.status for r in bundle.gates] == ["failed", "ran"]
    assert bundle.decision_action == HOLD


class _BlockingGatePlugin(FakeGatePlugin):
    """A gate whose evaluate blocks the event loop (no await to cancel at)."""

    def build(self, framework: Any) -> FakeGate:
        gate = super().build(framework)
        original = gate.evaluate

        async def evaluate(request: GateRequest) -> GateOutput:
            time.sleep(0.15)  # blocks the loop: wait_for cannot interrupt it
            return await original(request)

        gate.evaluate = evaluate  # type: ignore[method-assign]
        return gate


async def test_a_gate_that_blocks_the_loop_past_its_budget_fails_closed(
    gated_submission,
):
    bundle = await _run(
        gated_submission, gates=_compose(_BlockingGatePlugin()), gate_budget_ms=50
    )
    assert bundle.decision_action == HOLD
    [record] = bundle.gates
    assert record.status == "failed"
    assert record.reason is not None
    assert record.reason.startswith("exceeded its budget: returned after ")
    assert record.reason.endswith("(budget 50 ms)")


def _unstorable_outputs() -> list[Any]:
    def output(**fields: Any) -> GateOutput:
        return GateOutput(gate_id="fake", deterministic=False, verdict=None, **fields)

    def built(**fields: Any) -> GateOutput:  # skips validation, as a gate could
        return GateOutput.model_construct(
            gate_id="fake", deterministic=False, verdict=None, **fields
        )

    return [
        pytest.param(output(extra={"rationale": "fine\x00"}), "a NUL", id="nul"),
        pytest.param(
            output(evidence_snapshot={"llm": {"text\x00": "x"}}), "a NUL", id="nul-key"
        ),
        pytest.param(
            built(extra={"score": float("nan")}), "a non-finite number", id="nan"
        ),
        pytest.param(
            built(extra={"ua": "bad\ud800"}), "not valid Unicode", id="surrogate"
        ),
        pytest.param(
            built(evidence_snapshot={"at": datetime(2024, 1, 1, tzinfo=UTC)}),
            "cannot be serialized as JSON",
            id="not-json",
        ),
        pytest.param(
            built(evidence=[EvidenceRef(source_id="doc")]),
            "cannot be serialized as JSON",
            id="list-for-tuple",
        ),
    ]


@pytest.mark.parametrize(("override", "problem"), _unstorable_outputs())
async def test_an_output_the_ledger_cannot_store_fails_closed_and_is_written(
    gated_submission, override, problem
):
    gates = _compose(FakeGatePlugin())
    _gate(gates).output_override = override
    store = _RecordingStore()
    bundle = await _run(gated_submission, gates=gates, store=store)
    assert store.written == [bundle]
    assert bundle.decision_action == HOLD
    [record] = bundle.gates
    assert record.status == "failed"
    assert record.reason is not None
    assert record.reason.startswith("returned a malformed output: it holds ")
    assert problem in record.reason
    assert record.output is None
    assert storage_problem(bundle.model_dump(mode="json")) is None
    assert type(bundle).model_validate_json(bundle.model_dump_json()) == bundle


class _RaisingGatePlugin(FakeGatePlugin):
    """A gate that raises the given exception from evaluate."""

    def __init__(self, error: BaseException, gate_id: str = "fake") -> None:
        super().__init__(gate_id)
        self._error = error

    def build(self, framework: Any) -> FakeGate:
        gate = super().build(framework)
        error = self._error

        async def evaluate(request: GateRequest) -> GateOutput:
            await asyncio.sleep(0)
            raise error

        gate.evaluate = evaluate  # type: ignore[method-assign]
        return gate


async def test_a_reason_quoting_a_nul_from_the_gate_is_stored_escaped(
    gated_submission,
):
    store = _RecordingStore()
    bundle = await _run(
        gated_submission,
        gates=_compose(_RaisingGatePlugin(RuntimeError("upstream said: \x00"))),
        store=store,
    )
    assert store.written == [bundle]
    [record] = bundle.gates
    assert record.reason == "raised RuntimeError: upstream said: \\x00"
    assert storage_problem(bundle.model_dump(mode="json")) is None


async def test_a_timeout_the_gate_raises_itself_is_its_error_not_the_budget(
    gated_submission,
):
    gates = _compose(_RaisingGatePlugin(TimeoutError("read timed out (sanctions)")))
    request = GateRequest(
        decision_id="d",
        submission=gated_submission,
        gate_context=GateContext(gate_id="fake"),
        budget_ms=10_000,
    )
    invocation = await invoke_gate(gates["fake"], request)
    assert invocation.failure == "error"
    assert invocation.reason == "raised TimeoutError: read timed out (sanctions)"


class _CancelledHelperGatePlugin(FakeGatePlugin):
    """A gate awaiting a helper future that something else cancels."""

    def build(self, framework: Any) -> FakeGate:
        gate = super().build(framework)

        async def evaluate(request: GateRequest) -> GateOutput:
            helper = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_later(0.01, helper.cancel)
            return await helper

        gate.evaluate = evaluate  # type: ignore[method-assign]
        return gate


async def test_a_cancellation_inside_a_gate_fails_that_gate_and_the_decision_is_written(
    gated_submission,
):
    registry = _registry(_with_gates(_binding("fake"), _binding("other")))
    gates = _compose(_CancelledHelperGatePlugin(), FakeGatePlugin("other"))
    submission = _contexts(
        gated_submission,
        GateContext(gate_id="fake"),
        GateContext(
            gate_id="other",
            gate_config={"permitted_actions": ["CHALLENGE"], "delay_ms": 50},
        ),
    )
    store = _RecordingStore()
    bundle = await _run(submission, gates=gates, registry=registry, store=store)
    assert store.written == [bundle]
    fake, other = bundle.gates
    assert fake.status == "failed"
    assert fake.reason is not None
    assert fake.reason.startswith("raised CancelledError")
    assert other.status == "ran"
    assert bundle.decision_action == HOLD


class _IgnoresCancellationGatePlugin(FakeGatePlugin):
    """A gate that swallows its cancellation and answers anyway."""

    def build(self, framework: Any) -> FakeGate:
        gate = super().build(framework)
        original = gate.evaluate

        async def evaluate(request: GateRequest) -> GateOutput:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(1)
            return await original(request)

        gate.evaluate = evaluate  # type: ignore[method-assign]
        return gate


async def test_a_gate_that_ignores_its_cancellation_still_times_out(gated_submission):
    bundle = await _run(
        gated_submission,
        gates=_compose(_IgnoresCancellationGatePlugin()),
        gate_budget_ms=50,
    )
    [record] = bundle.gates
    assert record.status == "failed"
    assert record.reason is not None
    assert record.reason.startswith(
        "timed out after 50 ms (budget): it ignored its cancellation"
    )
    assert bundle.decision_action == HOLD


@pytest.mark.parametrize("blocking_first", [True, False])
async def test_a_gate_that_blocks_the_loop_is_not_charged_to_a_sibling_that_answered(
    gated_submission, blocking_first
):
    # Two mandatory gates under one budget: the blocking gate overruns it and
    # fails, but the lookup gate answered its BLOCK in time, whichever of the
    # two runs first — so the decision is BLOCK, as with the lookup alone.
    lookup = FakeLookupGatePlugin(
        gate_id="fast", listed={gated_submission.entity_id: BLOCK}
    )
    bindings = [_binding("fast", mandatory=True), _binding("slow", mandatory=True)]
    if blocking_first:
        bindings.reverse()
    registry = _registry(_with_gates(*bindings))
    submission = _contexts(
        gated_submission, GateContext(gate_id="fast"), GateContext(gate_id="slow")
    )
    bundle = await _run(
        submission,
        gates=_compose(lookup, _BlockingGatePlugin("slow")),
        registry=registry,
        gate_budget_ms=100,
    )
    fast, slow = bundle.gate("fast"), bundle.gate("slow")
    assert fast is not None
    assert slow is not None
    assert (fast.status, fast.enforced_action) == ("ran", BLOCK)
    assert slow.status == "failed"
    assert slow.reason is not None
    assert slow.reason.startswith("exceeded its budget")
    assert bundle.decision_action == BLOCK


async def test_the_callers_own_cancellation_still_propagates(gated_submission):
    gates = _compose(FakeGatePlugin())
    request = GateRequest(
        decision_id="d",
        submission=gated_submission,
        gate_context=GateContext(gate_id="fake", gate_config={"delay_ms": 5_000}),
        budget_ms=10_000,
    )
    task = asyncio.ensure_future(invoke_gate(gates["fake"], request))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_invoke_gate_reports_a_run_gate_as_ran(gated_submission):
    gates = _compose(FakeGatePlugin())
    request = GateRequest(
        decision_id="d",
        submission=gated_submission,
        gate_context=GateContext(gate_id="fake"),
        budget_ms=1_000,
    )
    invocation = await invoke_gate(gates["fake"], request)
    assert invocation.ran
    assert invocation.invoked
    assert invocation.failure is None
    assert isinstance(_gate(gates), FakeGate)


# ---------------------------------------------------------------------------
# Registration version and decide() (DR-31)
# ---------------------------------------------------------------------------


async def test_every_bundle_records_the_registration_version_it_was_decided_under(
    submission, gated_submission
):
    gates = _compose(FakeGatePlugin())
    for each in (submission, gated_submission):
        bundle = await _run(each, gates=gates)
        assert bundle.registration_version == registration_version(STUB_REGISTRATION)

    other = _with_gates(_binding("fake", mandatory=True))
    bundle = await _run(submission, gates=gates, registry=_registry(other))
    assert bundle.registration_version == registration_version(other)
    assert bundle.registration_version != registration_version(STUB_REGISTRATION)


async def test_decide_returns_the_bundle_without_writing_it(gated_submission):
    gates = _compose(FakeGatePlugin())

    bundle = await decide(
        submission=_scripted(gated_submission, permitted_actions=["CHALLENGE"]),
        gates=gates,
        registry=_registry(),
        ingestion_timestamp=_INGESTION_TS,
    )

    assert bundle.decision_action == CHALLENGE
    assert bundle.ingestion_timestamp == _INGESTION_TS
    assert len(_gate(gates).requests) == 1


# ---------------------------------------------------------------------------
# Identity (DR-32)
# ---------------------------------------------------------------------------


def _identity_service(budget_ms: int = 1_000, **script: Any):
    from app.identity import IdentityService, build_resolver
    from tests._stub_resolver.plugin import StubResolverPlugin

    composed = build_resolver(StubResolverPlugin(), None)  # type: ignore[arg-type]
    for name, value in script.items():
        setattr(composed.resolver, name, value)
    return IdentityService(composed, budget_ms=budget_ms), composed.resolver


def _naming(submission: Submission, key: str = "widget-12345") -> Submission:
    from core.identity import NativeSubject

    return submission.model_copy(
        update={
            "native_subject": NativeSubject(
                native_namespace="widget-registry", native_subject_id=key
            )
        }
    )


async def test_the_subject_is_resolved_before_any_gate_and_every_gate_sees_it(
    gated_submission,
):
    from tests._stub_resolver.plugin import owner_id

    events: list[str] = []
    identity, resolver = _identity_service()
    original = resolver.resolve

    def _resolve(namespace: str, key: str):
        events.append("resolve")
        return original(namespace, key)

    resolver.resolve = _resolve  # type: ignore[method-assign]
    gates = _compose(FakeGatePlugin())
    gate = _gate(gates)
    original_evaluate = gate.evaluate

    async def _evaluate(request: GateRequest):
        events.append("gate")
        return await original_evaluate(request)

    gate.evaluate = _evaluate
    bundle = await _run(_naming(gated_submission), gates=gates, identity=identity)

    assert events == ["resolve", "gate"]
    assert bundle.identity is not None
    assert bundle.entity_id == owner_id("owner-7")
    [request] = gate.requests
    assert request.identity == bundle.identity
    assert bundle.latency_breakdown["identity_ms"] >= 0


async def test_a_resolver_that_fails_never_refuses_the_decision(submission):
    store = _RecordingStore()
    identity, _ = _identity_service(error=RuntimeError("crosswalk down"))
    bundle = await _run(
        _naming(submission),
        gates=_compose(FakeGatePlugin()),
        store=store,
        identity=identity,
    )
    assert store.written == [bundle]
    assert bundle.decision_action == ALLOW  # the record is decided as usual
    assert bundle.identity is not None
    assert bundle.identity.provenance.method == "unresolved"
    assert bundle.entity_id == submission.entity_id
    assert "raised RuntimeError: crosswalk down" in (
        bundle.identity.provenance.reason or ""
    )


async def test_a_resolver_that_times_out_never_holds_the_decision_up(submission):
    store = _RecordingStore()
    identity, _ = _identity_service(budget_ms=20, delay_s=0.5)
    started = time.perf_counter()
    bundle = await _run(
        _naming(submission),
        gates=_compose(FakeGatePlugin()),
        store=store,
        identity=identity,
    )
    assert time.perf_counter() - started < 0.4
    assert store.written == [bundle]
    assert bundle.identity is not None
    assert "timed out after 20 ms" in (bundle.identity.provenance.reason or "")


async def test_without_an_identity_step_the_passthrough_records_the_subject(
    gated_submission,
):
    bundle = await _run(gated_submission, gates=_compose(FakeGatePlugin()))
    assert bundle.identity is not None
    assert bundle.identity.entity_id == gated_submission.entity_id
    assert bundle.identity.provenance.resolved_by == "passthrough"


async def test_a_native_subject_contradicting_its_declared_namespace_is_refused_first(
    submission,
):
    import uuid

    from core.identity import NativeNamespace

    declared = _revalidated(
        STUB_REGISTRATION.model_copy(
            update={
                "native_namespaces": (
                    NativeNamespace(
                        native_namespace="widget-registry",
                        namespace_uuid=uuid.uuid4(),
                        entity_type="widget",
                    ),
                )
            }
        )
    )
    store, gates = _RecordingStore(), _compose(FakeGatePlugin())
    identity, resolver = _identity_service()
    with pytest.raises(SubmissionContractError) as exc_info:
        await _run(
            _naming(submission),
            gates=gates,
            registry=_registry(declared),
            store=store,
            identity=identity,
        )
    assert exc_info.value.code == "NATIVE_SUBJECT_MISMATCH"
    assert resolver.calls == []
    assert _gate(gates).requests == []
    assert store.written == []
