"""The batch mock's eval adapter, datasets, and driver (``make eval-mocks``).

The adapter ships only ``scenarios/`` and ``robustness/`` datasets, which
load with the framework's loaders against ``AccountDay``. The driver runs
them through ``LocalClient`` over in-test services (the ``ledger`` fixture:
the mock's plugin and the real rules gate over the member's rule set, no
Postgres, no model):

- every consistency scenario's orderings give one stable result: a current
  account recorded ALLOW (no gate verdict to report), a flagged account
  held by the rules gate (its verdict and the rules that matched reported);
- ``run`` starts each ordering from fresh state and holds a lock, so the
  harness's concurrent dimensions never interleave two orderings;
- a forced schema failure holds without a write, and a forced fallback (the
  rules gate swapped for one that raises, for that call only) is failed
  closed and holds; every fallback trigger is flagged, so it reaches the
  gate, and every robustness case expects HOLD;
- ``run`` refuses an empty ordering and a day that repeats an event id.
"""

from __future__ import annotations

import asyncio

import pytest

from core.actions import DecisionAction
from eval.dimensions.consistency import PipelineRunResult, load_scenarios
from eval.dimensions.robustness import load_fallback_cases, load_schema_violation_cases
from mock_batch_reasoner.assembler import build_submission
from mock_batch_reasoner.eval.adapter import ADAPTER
from mock_batch_reasoner.eval.driver import MockBatchEvalDriver
from mock_batch_reasoner.rows import AccountDay
from mock_batch_reasoner.scorer import score_row

_DATASETS = ADAPTER.datasets_dir
_SCENARIOS = _DATASETS / "scenarios" / "batch_days.yaml"
_MALFORMED = _DATASETS / "robustness" / "malformed.yaml"
_FALLBACK = _DATASETS / "robustness" / "fallback.yaml"
#: Each consistency scenario's stable result.
_STABLE = {
    "mb-current-account": PipelineRunResult(decision_action=DecisionAction.ALLOW),
    "mb-flagged-account": PipelineRunResult(
        decision_action=DecisionAction.HOLD,
        confidence=1.0,
        rationale="rules gate: flagged-delinquent, flagged-past-due",
    ),
}


def _scenarios():
    return load_scenarios(_SCENARIOS, event_type=AccountDay)


def _bundles(ledger, event_id: str):
    """Every bundle the ledger wrote for ``event_id``, oldest first."""
    return [b for b in ledger.store.bundles() if b.submission.event_id == event_id]


def test_the_adapter_ships_only_scenario_and_robustness_datasets():
    # The RAG policy gate's eval runs every adapter that ships its kinds of
    # datasets; a mock ships only these two, so it never joins that eval.
    assert ADAPTER.reasoner_id == "mock-batch-reasoner"
    assert ADAPTER.event_type is AccountDay
    shipped = sorted(
        path.name
        for path in _DATASETS.iterdir()
        if path.is_dir() and not path.name.startswith("__")
    )
    assert shipped == ["robustness", "scenarios"]


def test_the_datasets_load_against_the_account_day():
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
        assert all(isinstance(e, AccountDay) for e in extract.events)

    schema_cases = load_schema_violation_cases(_MALFORMED, event_type=AccountDay)
    assert [c.case_id for c in schema_cases] == [
        "mb-malformed-current",
        "mb-malformed-flagged",
    ]
    fallback_cases = load_fallback_cases(_FALLBACK, event_type=AccountDay)
    assert [c.case_id for c in fallback_cases] == [
        "mb-fallback-flagged-current",
        "mb-fallback-flagged-charge-off",
    ]
    assert [len(c.priming_events) for c in fallback_cases] == [0, 1]


def test_every_fallback_trigger_reaches_the_gate_and_every_case_expects_hold():
    schema_cases = load_schema_violation_cases(_MALFORMED, event_type=AccountDay)
    fallback_cases = load_fallback_cases(_FALLBACK, event_type=AccountDay)

    assert {c.expected_action for c in [*schema_cases, *fallback_cases]} == {
        DecisionAction.HOLD
    }
    for case in fallback_cases:
        # Flagged: no bypass rule covers it, so it is gated, not recorded.
        assert case.event.needs_review
        assert build_submission(case.event, score_row(case.event)).mode == "gate"


async def test_every_consistency_scenario_gives_one_stable_result(ledger):
    driver = MockBatchEvalDriver(framework=ledger)

    for scenario in _scenarios():
        results = [await driver.run(o.events) for o in scenario.orderings]

        assert results == [_STABLE[scenario.scenario_id]] * len(results)


async def test_run_starts_each_ordering_from_fresh_state(ledger, row):
    driver = MockBatchEvalDriver(framework=ledger)
    current, flagged = (s.orderings[0] for s in _scenarios())
    await driver.run_event(row(1))
    assert set(driver.decided) == {"row-test-0001"}

    await driver.run(current.events)
    assert set(driver.decided) == {e.event_id for e in current.events}

    await driver.run(flagged.events)
    assert set(driver.decided) == {e.event_id for e in flagged.events}


async def test_concurrent_runs_never_interleave_their_orderings(ledger):
    driver = MockBatchEvalDriver(framework=ledger)
    scenarios = _scenarios()
    orderings = [o for s in scenarios for o in s.orderings]
    stable = [_STABLE[s.scenario_id] for s in scenarios for _ in s.orderings]

    results = await asyncio.gather(*(driver.run(o.events) for o in orderings))

    assert results == stable
    # The lock ran each ordering whole, in turn: the state is the last one's,
    # never a mix of two.
    assert set(driver.decided) == {e.event_id for e in orderings[-1].events}


async def test_a_forced_schema_failure_holds_without_a_write(ledger):
    driver = MockBatchEvalDriver(framework=ledger)

    for case in load_schema_violation_cases(_MALFORMED, event_type=AccountDay):
        written = len(ledger.store.rows)

        action = await driver.run_with_forced_schema_failure(case.event)

        assert action == case.expected_action == DecisionAction.HOLD
        assert len(ledger.store.rows) == written


async def test_a_forced_fallback_fails_the_gate_closed_and_the_swap_lasts_one_call(
    ledger, row
):
    driver = MockBatchEvalDriver(framework=ledger)
    gate = ledger.gates["rules"].gate

    for case in load_fallback_cases(_FALLBACK, event_type=AccountDay):
        for primer in case.priming_events:
            # The account's previous day, unflagged: recorded on its own.
            assert await driver.run_event(primer) == DecisionAction.CHALLENGE
            [primed] = _bundles(ledger, primer.event_id)
            assert primed.submission.mode == "record"

        action = await driver.run_with_forced_fallback(
            case.event, fallback_kind=case.fallback_kind
        )

        assert action == case.expected_action == DecisionAction.HOLD
        # The trigger was gated, and the gate it reached failed closed.
        [bundle] = _bundles(ledger, case.event.event_id)
        assert bundle.submission.mode == "gate"
        record = bundle.gate("rules")
        assert record is not None
        assert record.status == "failed"
        assert record.reason is not None
        assert case.fallback_kind in record.reason

    # The swap lasted one call: the real rules gate decides the next exception.
    assert driver.framework.gates["rules"].gate is gate
    flagged = row(
        1, balance=1_200.0, days_past_due=90, returned_payments=2, needs_review=True
    )
    assert await driver.run_event(flagged) == DecisionAction.BLOCK
    [bundle] = _bundles(ledger, flagged.event_id)
    record = bundle.gate("rules")
    assert record is not None
    assert record.status == "ran"


async def test_run_refuses_an_empty_ordering(ledger):
    with pytest.raises(ValueError, match="at least one event"):
        await MockBatchEvalDriver(framework=ledger).run([])


async def test_run_refuses_a_day_repeating_an_event_id(ledger, row):
    driver = MockBatchEvalDriver(framework=ledger)
    once = row(1)

    with pytest.raises(ValueError, match="two rows share the event id 'row-test-0001'"):
        await driver.run([once, once])

    assert ledger.store.rows == {}  # refused before anything was sent
