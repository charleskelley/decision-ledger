"""The stream mock's eval adapter, datasets, and driver (``make eval-mocks``).

The datasets load with the framework's loaders against ``TransferEvent``;
the driver runs them through ``LocalClient`` over in-test services (the
``ledger`` fixture: the mock's plugin and the scripted policy double, no
Postgres, no model):

- every consistency scenario's orderings give one stable action, because
  ``run`` starts each ordering from fresh state, and holds a lock, so the
  harness's concurrent dimensions never interleave two orderings;
- a forced schema failure holds without asking any gate or writing, and a
  forced fallback (the policy gate swapped for one that raises, for that
  call only) is failed closed and holds without the double being asked;
- ``reset_state`` forgets the per-account windows, the dedup set, and the
  watermark;
- ``run`` reports the policy verdict of a gated trigger (Tier 3 holds a
  fresh account's), and refuses an empty ordering and one the stream does
  not accept.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from app.enforcement.resolver import TIER3_NOVEL_ENTITY
from core.actions import DecisionAction
from eval.dimensions.consistency import PipelineRunResult, load_scenarios
from eval.dimensions.robustness import load_fallback_cases, load_schema_violation_cases
from mock_stream_reasoner.eval.adapter import ADAPTER
from mock_stream_reasoner.eval.driver import MockStreamEvalDriver
from mock_stream_reasoner.events import TransferEvent
from mock_stream_reasoner.testing import SCRIPTED_RATIONALE

_DATASETS = ADAPTER.datasets_dir
_SCENARIOS = _DATASETS / "scenarios" / "stream_accounts.yaml"
_MALFORMED = _DATASETS / "robustness" / "malformed.yaml"
_FALLBACK = _DATASETS / "robustness" / "fallback.yaml"
#: Each consistency scenario's stable action.
_STABLE = {
    "ms-steady-account": DecisionAction.ALLOW,
    "ms-surge-account": DecisionAction.BLOCK,
}


def _scenarios():
    return load_scenarios(_SCENARIOS, event_type=TransferEvent)


def _bundles(ledger, event_id: str):
    """Every bundle the ledger wrote for ``event_id``, oldest first."""
    return [b for b in ledger.store.bundles() if b.submission.event_id == event_id]


def test_the_adapter_ships_only_scenario_and_robustness_datasets():
    # The RAG policy gate's eval runs every adapter that ships its kinds of
    # datasets; a mock ships only these two, so it never joins that eval.
    assert ADAPTER.reasoner_id == "mock-stream-reasoner"
    assert ADAPTER.event_type is TransferEvent
    shipped = sorted(
        path.name
        for path in _DATASETS.iterdir()
        if path.is_dir() and not path.name.startswith("__")
    )
    assert shipped == ["robustness", "scenarios"]


def test_the_datasets_load_against_the_transfer_event():
    scenarios = _scenarios()
    assert [s.scenario_id for s in scenarios] == list(_STABLE)
    for scenario in scenarios:
        assert [o.ordering_id for o in scenario.orderings] == [
            "chronological",
            "reversed-precursors",
        ]
        chronological, reordered = scenario.orderings
        # The same events, the precursors reordered, the trigger last in both.
        assert chronological.events[-1] == reordered.events[-1]
        assert chronological.events[:-1] != reordered.events[:-1]
        assert sorted(e.event_id for e in chronological.events) == sorted(
            e.event_id for e in reordered.events
        )

    schema_cases = load_schema_violation_cases(_MALFORMED, event_type=TransferEvent)
    assert [c.case_id for c in schema_cases] == [
        "ms-malformed-fresh-large",
        "ms-malformed-small",
    ]
    fallback_cases = load_fallback_cases(_FALLBACK, event_type=TransferEvent)
    assert [c.case_id for c in fallback_cases] == [
        "ms-fallback-fresh-large",
        "ms-fallback-established-large",
    ]
    assert [len(c.priming_events) for c in fallback_cases] == [0, 3]
    assert {c.expected_action for c in [*schema_cases, *fallback_cases]} == {
        DecisionAction.HOLD
    }
    # Dated after the scenarios, so no robustness event trails the watermark
    # an ordering left behind.
    latest_scenario_event = max(
        e.timestamp for s in scenarios for o in s.orderings for e in o.events
    )
    robustness_events = [c.event for c in schema_cases] + [
        e for c in fallback_cases for e in (*c.priming_events, c.event)
    ]
    assert min(e.timestamp for e in robustness_events) > latest_scenario_event


async def test_every_consistency_scenario_gives_one_stable_action(ledger):
    driver = MockStreamEvalDriver(framework=ledger)

    for scenario in _scenarios():
        results = [await driver.run(o.events) for o in scenario.orderings]

        assert {r.decision_action for r in results} == {_STABLE[scenario.scenario_id]}
        # Both triggers fall in a band: recorded, so no gate verdict.
        assert all(r.confidence is None and r.rationale is None for r in results)


async def test_run_starts_each_ordering_from_fresh_state(ledger):
    driver = MockStreamEvalDriver(framework=ledger)
    ordering = _scenarios()[0].orderings[0]

    first = await driver.run(ordering.events)
    # The same event ids again: accepted only because the dedup set was reset.
    second = await driver.run(ordering.events)

    assert first == second == PipelineRunResult(decision_action=DecisionAction.ALLOW)
    features = [
        b.submission.reasoner_context.feature_set
        for b in _bundles(ledger, ordering.events[-1].event_id)
    ]
    # The windows were reset too: the trigger saw the same three precursors.
    assert len(features) == 2
    assert features[0] == features[1]
    assert features[0]["prior_events"] == 3


async def test_concurrent_runs_never_interleave_their_orderings(ledger):
    driver = MockStreamEvalDriver(framework=ledger)
    orderings = [o for s in _scenarios() for o in s.orderings]

    # Interleaved, the steady orderings' shared ids would be dropped as
    # duplicates and the surge's later events would leave the steady ones
    # late; the lock runs each ordering whole.
    results = await asyncio.gather(*(driver.run(o.events) for o in orderings))

    assert [r.decision_action for r in results] == [
        DecisionAction.ALLOW,
        DecisionAction.ALLOW,
        DecisionAction.BLOCK,
        DecisionAction.BLOCK,
    ]


async def test_a_forced_schema_failure_holds_without_a_gate_call_or_a_write(
    ledger, policy_gate
):
    driver = MockStreamEvalDriver(framework=ledger)

    for case in load_schema_violation_cases(_MALFORMED, event_type=TransferEvent):
        written = len(ledger.store.rows)
        asked = len(policy_gate.requests)

        action = await driver.run_with_forced_schema_failure(case.event)

        assert action == case.expected_action == DecisionAction.HOLD
        assert len(ledger.store.rows) == written
        assert len(policy_gate.requests) == asked


async def test_a_forced_fallback_holds_and_the_policy_double_is_not_asked(
    ledger, policy_gate, transfer
):
    driver = MockStreamEvalDriver(framework=ledger)

    for case in load_fallback_cases(_FALLBACK, event_type=TransferEvent):
        for primer in case.priming_events:
            assert await driver.run_event(primer) == DecisionAction.ALLOW
        asked = len(policy_gate.requests)

        action = await driver.run_with_forced_fallback(
            case.event, fallback_kind=case.fallback_kind
        )

        assert action == case.expected_action == DecisionAction.HOLD
        assert len(policy_gate.requests) == asked
        # The trigger was gated, and the gate it reached failed closed.
        [bundle] = _bundles(ledger, case.event.event_id)
        assert bundle.submission.mode == "gate"
        record = bundle.gate("policy")
        assert record is not None
        assert record.status == "failed"

    # The swap lasted one call: the next gated transfer asks the double.
    asked = len(policy_gate.requests)
    assert await driver.run_event(transfer(1, "acct-after", 450.0)) == (
        DecisionAction.HOLD
    )
    assert len(policy_gate.requests) == asked + 1


async def test_reset_state_forgets_the_windows_the_dedup_set_and_the_watermark(
    ledger, transfer
):
    driver = MockStreamEvalDriver(framework=ledger)
    first = transfer(1, "acct-reset", 80.0)
    await driver.run_event(first)
    await driver.run_event(transfer(2, "acct-reset", 90.0, minutes=20))
    with pytest.raises(ValueError, match="duplicate"):
        await driver.run_event(first)

    driver.reset_state()
    # Accepted again: its id is forgotten, and so is the watermark it now
    # trails by twenty minutes.
    await driver.run_event(first)

    priors = [
        b.submission.reasoner_context.feature_set["prior_events"]
        for b in _bundles(ledger, first.event_id)
    ]
    assert priors == [0, 0]  # the account's window was forgotten as well


async def test_run_reports_the_policy_verdict_of_a_gated_trigger(
    ledger, policy_gate, transfer
):
    # Three steady transfers, then one six times the baseline: between the
    # bands, so the mock asks the gate; the account is established.
    ordering = [
        transfer(1, "acct-gated", 100.0),
        transfer(2, "acct-gated", 110.0, minutes=20),
        transfer(3, "acct-gated", 90.0, minutes=40),
        transfer(4, "acct-gated", 600.0, minutes=60),
    ]

    result = await MockStreamEvalDriver(framework=ledger).run(ordering)

    assert result == PipelineRunResult(
        decision_action=DecisionAction.CHALLENGE,
        confidence=0.9,
        rationale=SCRIPTED_RATIONALE,
    )
    assert len(policy_gate.requests) == 1  # only the trigger was gated


async def test_a_fresh_accounts_large_first_transfer_is_held_by_tier3(ledger, transfer):
    trigger = transfer(1, "acct-fresh", 450.0)

    result = await MockStreamEvalDriver(framework=ledger).run([trigger])

    # The double's verdict is reported; Tier 3 held the decision anyway.
    assert result == PipelineRunResult(
        decision_action=DecisionAction.HOLD,
        confidence=0.9,
        rationale=SCRIPTED_RATIONALE,
    )
    [bundle] = _bundles(ledger, trigger.event_id)
    assert bundle.decision_level_rule == TIER3_NOVEL_ENTITY


async def test_run_refuses_an_empty_ordering(ledger):
    with pytest.raises(ValueError, match="at least one event"):
        await MockStreamEvalDriver(framework=ledger).run([])


async def test_run_refuses_an_ordering_the_stream_does_not_accept(ledger, transfer):
    driver = MockStreamEvalDriver(framework=ledger)
    once = transfer(1, "acct-refused", 80.0)
    with pytest.raises(
        ValueError, match=re.escape("did not accept 'evt-test-0001': duplicate")
    ):
        await driver.run([once, once])

    late = [
        transfer(2, "acct-refused", 80.0, minutes=30),
        transfer(3, "acct-refused", 85.0),
    ]
    with pytest.raises(
        ValueError,
        match=re.escape("did not accept 'evt-test-0003': dead_lettered (late)"),
    ):
        await driver.run(late)
