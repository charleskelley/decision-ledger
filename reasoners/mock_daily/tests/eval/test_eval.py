"""The daily mock's eval adapter, datasets, and driver (``make eval-mocks``).

The adapter ships only ``scenarios/`` and ``robustness/`` datasets, which
load with the framework's loaders against ``AccountSettlement``. The driver
runs them through ``LocalClient`` over in-test services (the ``ledger``
fixture: the mock's plugin and the real rules gate over the member's rule
set, no Postgres, no model):

- every consistency scenario's orderings give one stable result, the rules
  gate's verdict reported whether the trigger was recorded (an account
  within tolerance: ALLOW by the gate's default) or gated (an account 8%
  short: held, the rules that matched reported); a trigger the gate gave no
  verdict on is reported by its action alone;
- ``run`` starts each ordering from fresh state and holds a lock, so the
  harness's concurrent dimensions never interleave two orderings;
- a forced schema failure holds without a write, and a forced fallback (the
  rules gate swapped for one that raises, for that call only) is failed
  closed and holds; the gate is mandatory, so both hold a recorded row as
  well as an exception, and each robustness file has one of each;
- ``run`` refuses an empty ordering and a day that repeats an event id.
"""

from __future__ import annotations

import asyncio

import pytest

from app.enforcement.resolver import resolve_decision
from core.actions import DecisionAction
from eval.dimensions.consistency import PipelineRunResult, load_scenarios
from eval.dimensions.robustness import load_fallback_cases, load_schema_violation_cases
from mock_daily_reasoner.assembler import build_submission
from mock_daily_reasoner.compute import compute_drift
from mock_daily_reasoner.eval import driver as driver_module
from mock_daily_reasoner.eval.adapter import ADAPTER
from mock_daily_reasoner.eval.driver import MockDailyEvalDriver
from mock_daily_reasoner.plugin import PLUGIN
from mock_daily_reasoner.settlements import AccountSettlement
from rules_gate import RulesGatePlugin
from rules_gate.settings import RulesGateSettings
from tests._memory_ledger import memory_framework

_DATASETS = ADAPTER.datasets_dir
_SCENARIOS = _DATASETS / "scenarios" / "settlement_days.yaml"
_MALFORMED = _DATASETS / "robustness" / "malformed.yaml"
_FALLBACK = _DATASETS / "robustness" / "fallback.yaml"
#: Each consistency scenario's stable result.
_STABLE = {
    "md-within-tolerance": PipelineRunResult(
        decision_action=DecisionAction.ALLOW,
        confidence=1.0,
        rationale="rules gate: the default action",
    ),
    "md-major-drift": PipelineRunResult(
        decision_action=DecisionAction.HOLD,
        confidence=1.0,
        rationale="rules gate: drift-major, drift-minor",
    ),
}


def _scenarios():
    return load_scenarios(_SCENARIOS, event_type=AccountSettlement)


def _mode(event):
    """How the mock submits a row: ``record`` within tolerance, else ``gate``."""
    return build_submission(event, compute_drift(event)).mode


def _bundles(ledger, event_id: str):
    """Every bundle the ledger wrote for ``event_id``, oldest first."""
    return [b for b in ledger.store.bundles() if b.submission.event_id == event_id]


def test_the_adapter_ships_only_scenario_and_robustness_datasets():
    # The RAG policy gate's eval runs every adapter that ships its kinds of
    # datasets; a mock ships only these two, so it never joins that eval.
    assert ADAPTER.reasoner_id == "mock-daily-reasoner"
    assert ADAPTER.event_type is AccountSettlement
    shipped = sorted(
        path.name
        for path in _DATASETS.iterdir()
        if path.is_dir() and not path.name.startswith("__")
    )
    assert shipped == ["robustness", "scenarios"]


def test_the_datasets_load_against_the_account_settlement():
    scenarios = _scenarios()
    assert [s.scenario_id for s in scenarios] == list(_STABLE)
    for scenario in scenarios:
        assert [o.ordering_id for o in scenario.orderings] == [
            "extract-order",
            "reversed-precursors",
        ]
        extract, reordered = scenario.orderings
        # The same rows, the precursors reordered, the trigger last in both.
        assert extract.events[-1] == reordered.events[-1]
        assert extract.events[:-1] != reordered.events[:-1]
        assert sorted(e.event_id for e in extract.events) == sorted(
            e.event_id for e in reordered.events
        )
        assert all(isinstance(e, AccountSettlement) for e in extract.events)
        # The day holds records and exceptions alike.
        assert {_mode(e) for e in extract.events} == {"record", "gate"}

    schema_cases = load_schema_violation_cases(_MALFORMED, event_type=AccountSettlement)
    assert [c.case_id for c in schema_cases] == [
        "md-malformed-within-tolerance",
        "md-malformed-exception",
    ]
    fallback_cases = load_fallback_cases(_FALLBACK, event_type=AccountSettlement)
    assert [c.case_id for c in fallback_cases] == [
        "md-fallback-within-tolerance",
        "md-fallback-severe-drift",
    ]
    assert [len(c.priming_events) for c in fallback_cases] == [0, 1]
    [primer] = fallback_cases[1].priming_events
    # The same account's previous day.
    assert primer.account_id == fallback_cases[1].event.account_id
    assert primer.as_of < fallback_cases[1].event.as_of


def test_each_robustness_file_holds_a_recorded_row_and_an_exception_expecting_hold():
    schema_cases = load_schema_violation_cases(_MALFORMED, event_type=AccountSettlement)
    fallback_cases = load_fallback_cases(_FALLBACK, event_type=AccountSettlement)

    assert {c.expected_action for c in [*schema_cases, *fallback_cases]} == {
        DecisionAction.HOLD
    }
    assert [_mode(c.event) for c in schema_cases] == ["record", "gate"]
    assert [_mode(c.event) for c in fallback_cases] == ["record", "gate"]
    assert [c.fallback_kind for c in fallback_cases] == [
        "ruleset_unreadable",
        "gate_error",
    ]


async def test_every_consistency_scenario_gives_one_stable_result(ledger):
    driver = MockDailyEvalDriver(framework=ledger)

    for scenario in _scenarios():
        results = [await driver.run(o.events) for o in scenario.orderings]

        assert results == [_STABLE[scenario.scenario_id]] * len(results)


async def test_a_trigger_the_gate_gave_no_verdict_on_is_reported_by_its_action(
    tmp_path,
):
    empty = tmp_path / "rulesets"
    empty.mkdir()
    gate = RulesGatePlugin(RulesGateSettings(rulesets_dir=str(empty)))
    driver = MockDailyEvalDriver(framework=memory_framework([PLUGIN], [gate]))
    within, _ = (s.orderings[0] for s in _scenarios())

    result = await driver.run(within.events)

    assert result == PipelineRunResult(decision_action=DecisionAction.HOLD)


async def test_run_starts_each_ordering_from_fresh_state(ledger, settlement):
    driver = MockDailyEvalDriver(framework=ledger)
    within, major = (s.orderings[0] for s in _scenarios())
    assert await driver.run_event(settlement(1)) == DecisionAction.ALLOW
    assert set(driver.decided) == {"set-test-0001"}

    await driver.run(within.events)
    assert set(driver.decided) == {e.event_id for e in within.events}

    await driver.run(major.events)
    assert set(driver.decided) == {e.event_id for e in major.events}


async def test_concurrent_runs_never_interleave_their_orderings(ledger):
    driver = MockDailyEvalDriver(framework=ledger)
    scenarios = _scenarios()
    orderings = [o for s in scenarios for o in s.orderings]
    stable = [_STABLE[s.scenario_id] for s in scenarios for _ in s.orderings]

    results = await asyncio.gather(*(driver.run(o.events) for o in orderings))

    assert results == stable
    # The lock ran each ordering whole, in turn: the state is the last one's,
    # never a mix of two.
    assert set(driver.decided) == {e.event_id for e in orderings[-1].events}


async def test_a_forced_schema_failure_holds_a_record_and_an_exception_unwritten(
    ledger, monkeypatch
):
    driver = MockDailyEvalDriver(framework=ledger)
    enforced = []

    def recording(**kwargs):
        decision = resolve_decision(**kwargs)
        enforced.append((kwargs, decision))
        return decision

    monkeypatch.setattr(driver_module, "resolve_decision", recording)

    for case in load_schema_violation_cases(_MALFORMED, event_type=AccountSettlement):
        written = len(ledger.store.rows)

        action = await driver.run_with_forced_schema_failure(case.event)

        assert action == case.expected_action == DecisionAction.HOLD
        assert len(ledger.store.rows) == written

    # Each row is enforced in its own mode, against a mandatory rules gate
    # with no verdict: the record keeps its cited rule, which holds, and the
    # gate's failure alone holds it; the exception has no rule to cite.
    (record_kwargs, record), (gate_kwargs, gated) = enforced
    assert record_kwargs["submission"].mode == "record"
    assert record_kwargs["bypass_check"] is not None
    assert record_kwargs["bypass_check"].holds
    assert record_kwargs["bypass_check"].rule.rule_id == "within-tolerance"
    assert gate_kwargs["submission"].mode == "gate"
    assert gate_kwargs["bypass_check"] is None
    for kwargs in (record_kwargs, gate_kwargs):
        [gate_record] = kwargs["records"]
        assert (gate_record.gate_id, gate_record.mandatory) == ("rules", True)
        assert gate_record.output is not None
        assert gate_record.output.verdict is None
    assert record.override_log[-1] == (
        "combinator: most_restrictive(proposed=ALLOW, mandatory:rules=HOLD) → HOLD"
    )
    assert gated.decision_action == DecisionAction.HOLD


async def test_a_forced_fallback_fails_the_gate_closed_on_a_record_and_an_exception(
    ledger, settlement
):
    driver = MockDailyEvalDriver(framework=ledger)
    gate = ledger.gates["rules"].gate

    for case in load_fallback_cases(_FALLBACK, event_type=AccountSettlement):
        for primer in case.priming_events:
            # The account's previous day, within tolerance: recorded on its
            # own, by the real rules gate.
            assert await driver.run_event(primer) == DecisionAction.ALLOW
            [primed] = _bundles(ledger, primer.event_id)
            assert primed.submission.mode == "record"

        action = await driver.run_with_forced_fallback(
            case.event, fallback_kind=case.fallback_kind
        )

        assert action == case.expected_action == DecisionAction.HOLD
        # Recorded or gated, the mandatory gate was invoked and failed closed.
        [bundle] = _bundles(ledger, case.event.event_id)
        assert bundle.submission.mode == _mode(case.event)
        record = bundle.gate("rules")
        assert record is not None
        assert (record.status, record.mandatory) == ("failed", True)
        assert record.reason is not None
        assert case.fallback_kind in record.reason

    # The swap lasted one call: the real rules gate decides the next rows.
    assert driver.framework.gates["rules"].gate is gate
    severe = settlement(1, target=1_000.0, actual=1_600.0)  # 60% over
    assert await driver.run_event(severe) == DecisionAction.BLOCK
    assert await driver.run_event(settlement(2)) == DecisionAction.ALLOW
    for event_id in ("set-test-0001", "set-test-0002"):
        [bundle] = _bundles(ledger, event_id)
        record = bundle.gate("rules")
        assert record is not None
        assert record.status == "ran"


async def test_run_refuses_an_empty_ordering(ledger):
    with pytest.raises(ValueError, match="at least one event"):
        await MockDailyEvalDriver(framework=ledger).run([])


async def test_run_refuses_a_day_repeating_an_event_id(ledger, settlement):
    driver = MockDailyEvalDriver(framework=ledger)
    once = settlement(1)

    with pytest.raises(ValueError, match="two rows share the event id 'set-test-0001'"):
        await driver.run([once, once])

    assert ledger.store.rows == {}  # refused before anything was sent
