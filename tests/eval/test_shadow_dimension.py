"""Behavioral tests for the shadow evaluation dimension (DR-30).

The dimension re-decides a sample of recorded decisions as if the reasoner
had asked for a gate — advisory gates run offline on the stored submission
and contexts, stored mandatory outcomes kept, decision-level rules applied
in gate mode — and compares each hypothetical action with the recorded one.
It is report-only (it passes whenever it runs) and writes nothing.

Recorded bundles come from the real ``app.submit.submit`` with the stub
reasoner and the scripted fake gates; no infrastructure or LLM is involved.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.audit.store import RecordedSample
from app.gates import ComposedGate
from app.reasoner_registry import StaticReasonerRegistry
from app.submit import submit
from core.actions import DecisionAction
from core.eval.metrics import EvalDimension, ShadowMetrics
from core.observation import GateBinding, GateContext, ReasonerRegistration
from eval.dimensions.shadow import ShadowDimension
from tests._fake_gate.plugin import FakeGatePlugin, FakeLookupGatePlugin
from tests._stub_reasoner.assembler import build_submission
from tests._stub_reasoner.events import StubEvent
from tests._stub_reasoner.registry import STUB_REGISTRATION

if TYPE_CHECKING:
    from core.bundle import DecisionBundle
    from core.observation import ReasonerContext

ALLOW, HOLD, BLOCK = DecisionAction.ALLOW, DecisionAction.HOLD, DecisionAction.BLOCK


class _Store:
    def __init__(self) -> None:
        self.written: list[DecisionBundle] = []

    def write(self, bundle: DecisionBundle) -> None:
        self.written.append(bundle)


class _Source:
    """A ``RecordedBundleSource`` over a fixed sample; records each query."""

    def __init__(self, bundles: list[DecisionBundle], unloadable: int = 0) -> None:
        self._sample = RecordedSample(bundles=tuple(bundles), unloadable=unloadable)
        self.queries: list[tuple[str, int, datetime | None]] = []

    def recent_recorded(
        self, reasoner_id: str, *, limit: int, since: datetime | None = None
    ) -> RecordedSample:
        self.queries.append((reasoner_id, limit, since))
        return self._sample


def _compose(*plugins: Any) -> dict[str, ComposedGate]:
    return {
        p.gate_id: ComposedGate(plugin=p, gate=p.build(framework=None))  # type: ignore[arg-type]
        for p in plugins
    }


def _context(base: ReasonerContext, score: float, *, thin: bool = False):
    return base.model_copy(
        update={
            "label_value": score,
            "feature_set": {**base.feature_set, "thin_history": thin},
        }
    )


async def _record(
    reasoner_context: ReasonerContext,
    *,
    score: float = 0.05,
    thin: bool = False,
    script: dict[str, Any] | None = None,
    registration: ReasonerRegistration = STUB_REGISTRATION,
    gates: dict[str, ComposedGate] | None = None,
    contexts: list[GateContext] | None = None,
) -> DecisionBundle:
    """A recorded bundle written by the real ``submit``."""
    event = StubEvent(
        event_id=f"evt-{score}-{thin}",
        timestamp=datetime(2024, 1, 15, tzinfo=UTC),
        widget_id="widget-shadow",
    )
    submission = build_submission(
        event,
        _context(reasoner_context, score, thin=thin),
        gate_contexts=(
            contexts
            if contexts is not None
            else [GateContext(gate_id="fake", gate_config=script or {})]
        ),
        registration=registration,
    )
    assert submission.mode == "record"
    return await submit(
        submission=submission,
        gates=gates if gates is not None else _compose(FakeGatePlugin()),
        store=_Store(),  # type: ignore[arg-type]
        registry=StaticReasonerRegistry([registration]),
        ingestion_timestamp=event.timestamp,
    )


def _dimension(bundles, *, gates=None, registration=STUB_REGISTRATION, **kw):
    return ShadowDimension(
        source=_Source(bundles),
        gates=gates if gates is not None else _compose(FakeGatePlugin()),
        registry=StaticReasonerRegistry([registration]),
        reasoner_id=registration.reasoner_id,
        **kw,
    )


def _metrics(run) -> ShadowMetrics:
    assert isinstance(run.metrics, ShadowMetrics)
    return run.metrics


# ---------------------------------------------------------------------------
# Comparisons
# ---------------------------------------------------------------------------


async def test_a_record_the_gate_agrees_with_counts_as_agreement(reasoner_context):
    bundle = await _record(reasoner_context, script={"permitted_actions": ["ALLOW"]})
    run = await _dimension([bundle]).evaluate()
    metrics = _metrics(run)
    assert (metrics.num_decisions, metrics.num_compared) == (1, 1)
    assert metrics.agreement_rate == 1.0
    assert run.result.dimension == EvalDimension.SHADOW
    assert run.result.passed is True


async def test_a_record_the_gate_would_block_counts_as_an_escalation(
    reasoner_context,
):
    bundle = await _record(reasoner_context, script={"permitted_actions": ["BLOCK"]})
    assert bundle.decision_action == ALLOW
    metrics = _metrics(await _dimension([bundle]).evaluate())
    assert (metrics.agreement_rate, metrics.escalation_rate) == (0.0, 1.0)


async def test_a_record_the_gate_would_allow_counts_as_a_relaxation(
    reasoner_context,
):
    bundle = await _record(
        reasoner_context, score=0.97, script={"permitted_actions": ["ALLOW"]}
    )
    assert bundle.decision_action == BLOCK
    metrics = _metrics(await _dimension([bundle]).evaluate())
    assert metrics.relaxation_rate == 1.0


async def test_the_novel_entity_tier_applies_as_in_gate_mode(reasoner_context):
    # Recorded ALLOW for a thin-history widget: gated, Tier 3 would have
    # held it for review even though the gate allows it.
    bundle = await _record(
        reasoner_context, thin=True, script={"permitted_actions": ["ALLOW"]}
    )
    assert bundle.decision_action == ALLOW
    metrics = _metrics(await _dimension([bundle]).evaluate())
    assert metrics.escalation_rate == 1.0


async def test_stored_mandatory_outcomes_are_kept_not_rerun(reasoner_context):
    # A mandatory lookup gate raised the record to HOLD; the shadow keeps
    # that outcome, so an allowing advisory gate is not a "relaxation".
    lookup = FakeLookupGatePlugin()
    registration = ReasonerRegistration.model_validate_json(
        STUB_REGISTRATION.model_copy(
            update={
                "gates": (
                    GateBinding(gate_id="fake-lookup", mandatory=True),
                    GateBinding(gate_id="fake"),
                )
            }
        ).model_dump_json()
    )
    gates = _compose(FakeGatePlugin(), lookup)
    event_entity = StubEvent(
        event_id="e",
        timestamp=datetime(2024, 1, 15, tzinfo=UTC),
        widget_id="widget-shadow",
    ).entity_id
    lookup.listed[event_entity] = HOLD
    bundle = await _record(
        reasoner_context,
        registration=registration,
        gates=gates,
        contexts=[GateContext(gate_id="fake-lookup"), GateContext(gate_id="fake")],
    )
    assert bundle.decision_action == HOLD
    lookup.listed.clear()  # the shadow must not consult the lookup again
    metrics = _metrics(
        await _dimension([bundle], gates=gates, registration=registration).evaluate()
    )
    assert metrics.agreement_rate == 1.0
    assert lookup.built[0].requests[1:] == []


async def test_a_gate_that_fails_closed_is_counted(reasoner_context):
    bundle = await _record(reasoner_context, script={"raise_error": True})
    metrics = _metrics(await _dimension([bundle]).evaluate())
    assert metrics.gate_failure_rate == 1.0
    assert metrics.escalation_rate == 1.0  # the failed gate holds


async def test_rates_are_over_the_compared_decisions(reasoner_context):
    agree = await _record(reasoner_context, script={"permitted_actions": ["ALLOW"]})
    escalate = await _record(
        reasoner_context, score=0.1, script={"permitted_actions": ["HOLD"]}
    )
    metrics = _metrics(await _dimension([agree, escalate]).evaluate())
    assert (metrics.agreement_rate, metrics.escalation_rate) == (0.5, 0.5)


# ---------------------------------------------------------------------------
# Sampling and reporting
# ---------------------------------------------------------------------------


async def test_a_record_without_advisory_gates_is_not_compared(reasoner_context):
    registration = ReasonerRegistration.model_validate_json(
        STUB_REGISTRATION.model_copy(update={"gates": ()}).model_dump_json()
    )
    bundle = await _record(reasoner_context, registration=registration, contexts=[])
    metrics = _metrics(await _dimension([bundle], registration=registration).evaluate())
    assert (metrics.num_decisions, metrics.num_compared) == (1, 0)
    assert metrics.agreement_rate == 0.0


async def test_an_empty_sample_passes_with_zero_rates():
    run = await _dimension([]).evaluate()
    metrics = _metrics(run)
    assert metrics == ShadowMetrics(
        num_decisions=0,
        num_compared=0,
        agreement_rate=0.0,
        escalation_rate=0.0,
        relaxation_rate=0.0,
        gate_failure_rate=0.0,
        unloadable=0,
    )
    assert run.result.passed is True
    assert run.result.num_samples == 0


async def test_the_dimension_is_report_only(reasoner_context):
    bundle = await _record(reasoner_context, script={"permitted_actions": ["BLOCK"]})
    run = await _dimension([bundle]).evaluate()
    assert run.result.passed is True
    assert run.result.threshold_violations == []
    assert run.result.metrics["escalation_rate"] == 1.0


async def test_the_sample_is_bounded_and_scoped_to_the_run():
    since = datetime(2026, 10, 14, tzinfo=UTC)
    source = _Source([])
    dimension = ShadowDimension(
        source=source,
        gates={},
        registry=StaticReasonerRegistry([STUB_REGISTRATION]),
        reasoner_id="stub-reasoner",
        sample_size=3,
        since=since,
    )
    await dimension.evaluate()
    assert source.queries == [("stub-reasoner", 3, since)]


async def test_unloadable_rows_are_reported(reasoner_context):
    source = _Source([], unloadable=2)
    dimension = ShadowDimension(
        source=source,
        gates={},
        registry=StaticReasonerRegistry([STUB_REGISTRATION]),
        reasoner_id="stub-reasoner",
    )
    assert _metrics(await dimension.evaluate()).unloadable == 2


async def test_the_gate_sees_the_stored_context_and_a_gate_mode_submission(
    reasoner_context,
):
    gates = _compose(FakeGatePlugin())
    bundle = await _record(
        reasoner_context, script={"permitted_actions": ["CHALLENGE"]}, gates=gates
    )
    await _dimension([bundle], gates=gates).evaluate()
    [request] = gates["fake"].gate.requests  # type: ignore[attr-defined]
    assert request.decision_id == bundle.decision_id
    assert request.gate_context == bundle.submission.gate_contexts[0]
    assert request.submission.mode == "gate"
    assert request.submission.bypass is None
    assert request.submission.proposed_action is None


def test_the_dimension_runs_after_the_others_under_its_own_kind():
    assert ShadowDimension.runs_after_others is True
    assert ShadowDimension.kind == EvalDimension.SHADOW
