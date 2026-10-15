"""The example's eval adapter, datasets, and driver.

The datasets load with the framework's loaders against ``WidgetEvent``; the
driver runs them through ``LocalClient`` over in-test services: the
consistency scenarios are stable, a forced schema failure holds, and a
forced fallback (the policy gate replaced by one that raises) is failed
closed by the ledger and holds.
"""

from __future__ import annotations

from core.actions import DecisionAction
from eval.dimensions.consistency import load_scenarios
from eval.dimensions.robustness import load_fallback_cases, load_schema_violation_cases
from example_reasoner.eval.adapter import ADAPTER
from example_reasoner.eval.driver import ExampleEvalDriver
from example_reasoner.events import WidgetEvent

_DATASETS = ADAPTER.datasets_dir


def test_the_datasets_load_against_the_event_type():
    assert ADAPTER.event_type is WidgetEvent
    scenarios = load_scenarios(
        _DATASETS / "scenarios" / "recorded_widgets.yaml", event_type=WidgetEvent
    )
    assert [s.scenario_id for s in scenarios] == [
        "ex-steady-widget",
        "ex-broken-widget",
    ]
    assert (
        len(
            load_schema_violation_cases(
                _DATASETS / "robustness" / "malformed.yaml", event_type=WidgetEvent
            )
        )
        == 2
    )
    assert (
        len(
            load_fallback_cases(
                _DATASETS / "robustness" / "fallback.yaml", event_type=WidgetEvent
            )
        )
        == 2
    )


async def test_the_consistency_scenarios_are_stable(ledger):
    driver = ExampleEvalDriver(framework=ledger)
    expected = {"ex-steady-widget": "ALLOW", "ex-broken-widget": "BLOCK"}
    for scenario in load_scenarios(
        _DATASETS / "scenarios" / "recorded_widgets.yaml", event_type=WidgetEvent
    ):
        actions = {
            (await driver.run(ordering.events)).decision_action
            for ordering in scenario.orderings
        }
        assert actions == {DecisionAction(expected[scenario.scenario_id])}


async def test_robustness_cases_hold(ledger, policy_gate):
    driver = ExampleEvalDriver(framework=ledger)
    for case in load_schema_violation_cases(
        _DATASETS / "robustness" / "malformed.yaml", event_type=WidgetEvent
    ):
        assert await driver.run_with_forced_schema_failure(case.event) == (
            case.expected_action
        )
    for case in load_fallback_cases(
        _DATASETS / "robustness" / "fallback.yaml", event_type=WidgetEvent
    ):
        action = await driver.run_with_forced_fallback(
            case.event, fallback_kind=case.fallback_kind
        )
        assert action == case.expected_action
    assert policy_gate.requests == []  # the composed gate was swapped out


async def test_run_reports_the_gates_rationale(ledger):
    from example_reasoner.__main__ import sample_event

    result = await ExampleEvalDriver(framework=ledger).run([sample_event("mid")])
    assert result.decision_action == DecisionAction.CHALLENGE
    assert result.confidence == 0.9
    assert result.rationale is not None
