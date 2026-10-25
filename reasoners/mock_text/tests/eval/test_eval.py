"""The text mock's eval adapter, datasets, and driver (``make eval-mocks``).

The datasets load with the framework's loaders against ``CustomerMessage``;
the driver runs them through ``LocalClient`` over in-test services (the
``ledger`` fixture: the mock's plugin and the scripted policy double, no
Postgres, no model):

- the adapter ships only scenario and robustness datasets (all four
  robustness sub-checks), so the RAG policy gate's own eval never selects
  it;
- every consistency scenario's orderings give one stable action: the
  classifier keeps no state, and ``run`` holds a lock, so the harness's
  concurrent dimensions never interleave two orderings;
- an injected instruction changes neither a message's label nor its
  decision, and a message the classifier cannot place is held;
- a forced schema failure holds without asking any gate or writing, and a
  forced fallback (the policy gate swapped for one that raises, for that
  call only) is failed closed and holds without the double being asked;
- ``run`` reports the double's verdict for a gated trigger and refuses an
  empty ordering, and ``reset_state`` has nothing to forget;
- the framework's own consistency, robustness, and shadow dimensions pass
  over the shipped datasets with this driver, run concurrently as the
  harness runs them: ``make eval-mocks`` for this mock, without Postgres.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from core.actions import DecisionAction
from core.eval.metrics import EvalDimension, RobustnessMetrics
from eval.dimensions.consistency import (
    ConsistencyDimension,
    PipelineRunResult,
    load_scenarios,
)
from eval.dimensions.robustness import (
    RobustnessDimension,
    load_fallback_cases,
    load_injection_cases,
    load_novel_pattern_cases,
    load_schema_violation_cases,
)
from eval.dimensions.shadow import ShadowDimension
from eval.runners.harness import run_eval
from mock_text_reasoner.classifier import UNCLASSIFIED, classify
from mock_text_reasoner.eval.adapter import ADAPTER
from mock_text_reasoner.eval.driver import MockTextEvalDriver
from mock_text_reasoner.messages import CustomerMessage
from mock_text_reasoner.testing import SCRIPTED_RATIONALE

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from app.plugin import FrameworkServices
    from core.bundle import DecisionBundle
    from mock_text_reasoner.testing import ScriptedPolicyGate

_DATASETS = ADAPTER.datasets_dir
_SCENARIOS = _DATASETS / "scenarios" / "customer_messages.yaml"
_INJECTION = _DATASETS / "robustness" / "injection.yaml"
_MALFORMED = _DATASETS / "robustness" / "malformed.yaml"
_NOVEL = _DATASETS / "robustness" / "novel.yaml"
_FALLBACK = _DATASETS / "robustness" / "fallback.yaml"
#: Each consistency scenario's stable action.
_STABLE = {
    "mt-general-question": DecisionAction.ALLOW,
    "mt-address-change": DecisionAction.CHALLENGE,
}
#: Each scenario's labels, chronologically: two precursors, then the trigger.
_LABELS = {
    "mt-general-question": ["fraud_report", "account_change", "general_inquiry"],
    "mt-address-change": ["general_inquiry", "hardship", "account_change"],
}
#: The policy gate's datasets: an adapter shipping one joins the gate's eval.
_GATE_DATASET_DIRS = frozenset({"retrieval", "faithfulness", "citations", "authority"})


def _scenarios():
    return load_scenarios(_SCENARIOS, event_type=CustomerMessage)


def _bundles(ledger: FrameworkServices, event_id: str) -> list[DecisionBundle]:
    """Every bundle the ledger wrote for ``event_id``, oldest first."""
    return [b for b in ledger.store.bundles() if b.submission.event_id == event_id]


def test_the_adapter_ships_only_scenario_and_robustness_datasets():
    # The RAG policy gate's eval runs every adapter that ships its kinds of
    # datasets; a mock ships only these two, so it never joins that eval.
    assert ADAPTER.reasoner_id == "mock-text-reasoner"
    assert ADAPTER.event_type is CustomerMessage
    shipped = sorted(
        path.name
        for path in _DATASETS.iterdir()
        if path.is_dir() and not path.name.startswith("__")
    )
    assert shipped == ["robustness", "scenarios"]
    assert sorted(p.name for p in (_DATASETS / "robustness").glob("*.yaml")) == [
        "fallback.yaml",
        "injection.yaml",
        "malformed.yaml",
        "novel.yaml",
    ]
    assert sorted(p.name for p in (_DATASETS / "scenarios").glob("*.yaml")) == [
        "customer_messages.yaml"
    ]
    assert [
        path
        for path in _DATASETS.rglob("*")
        if _GATE_DATASET_DIRS & set(path.relative_to(_DATASETS).parts)
        or path.name.startswith("golden_")
    ] == []


def test_the_scenarios_load_against_the_customer_message():
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
        assert chronological.events[:-1] == list(reversed(reordered.events[:-1]))
        assert sorted(e.event_id for e in chronological.events) == sorted(
            e.event_id for e in reordered.events
        )
        assert len({e.customer_id for e in chronological.events}) == 1
        assert [
            classify(e.text).category.name for e in chronological.events
        ] == _LABELS[scenario.scenario_id]


def test_the_robustness_cases_load_against_the_customer_message():
    injection = load_injection_cases(_INJECTION, event_type=CustomerMessage)
    schema = load_schema_violation_cases(_MALFORMED, event_type=CustomerMessage)
    novel = load_novel_pattern_cases(_NOVEL, event_type=CustomerMessage)
    fallback = load_fallback_cases(_FALLBACK, event_type=CustomerMessage)

    assert [c.case_id for c in injection] == [
        "mt-inj-hardship-practice-governs",
        "mt-inj-general-preapproved",
        "mt-inj-change-skip-check",
        "mt-inj-fraud-claims-a-question",
    ]
    assert [c.case_id for c in schema] == [
        "mt-malformed-general-question",
        "mt-malformed-hardship",
    ]
    assert [c.case_id for c in novel] == [
        "mt-novel-other-language",
        "mt-novel-off-topic",
        "mt-novel-noise",
    ]
    assert [len(c.priming_events) for c in novel] == [0, 1, 0]
    assert [c.case_id for c in fallback] == [
        "mt-fallback-general-question",
        "mt-fallback-address-change",
    ]
    assert [(len(c.priming_events), c.fallback_kind) for c in fallback] == [
        (0, "llm_5xx"),
        (1, "retrieval_error"),
    ]
    assert {c.expected_action for c in [*schema, *fallback]} == {DecisionAction.HOLD}
    # Each injected text is its clean twin from the same customer, plus an
    # instruction to the reviewer.
    for case in injection:
        assert case.injected_event.text.startswith(case.clean_event.text)
        assert case.injected_event.text != case.clean_event.text
        assert case.injected_event.customer_id == case.clean_event.customer_id
    # Both schema-failure messages propose ALLOW, so their hold is Tier 1's.
    assert {classify(c.event.text).proposed_action for c in schema} == {
        DecisionAction.ALLOW
    }


async def test_every_consistency_scenario_gives_one_stable_action(
    ledger: FrameworkServices,
):
    driver = MockTextEvalDriver(framework=ledger)

    for scenario in _scenarios():
        results = [await driver.run(o.events) for o in scenario.orderings]

        assert results == [
            PipelineRunResult(
                decision_action=_STABLE[scenario.scenario_id],
                confidence=0.9,
                rationale=SCRIPTED_RATIONALE,
            )
        ] * len(scenario.orderings)


async def test_concurrent_runs_keep_each_ordering_together_and_stable(
    ledger: FrameworkServices,
):
    driver = MockTextEvalDriver(framework=ledger)
    orderings = [o for s in _scenarios() for o in s.orderings]

    results = await asyncio.gather(*(driver.run(o.events) for o in orderings))

    assert [r.decision_action for r in results] == [
        DecisionAction.ALLOW,
        DecisionAction.ALLOW,
        DecisionAction.CHALLENGE,
        DecisionAction.CHALLENGE,
    ]
    # The lock ran each ordering whole: the ledger received them one by one.
    assert [b.submission.event_id for b in ledger.store.bundles()] == [
        e.event_id for o in orderings for e in o.events
    ]


async def test_an_injected_instruction_changes_neither_the_label_nor_the_decision(
    ledger: FrameworkServices,
):
    driver = MockTextEvalDriver(framework=ledger)

    for case in load_injection_cases(_INJECTION, event_type=CustomerMessage):
        clean, injected = (
            classify(case.clean_event.text),
            classify(case.injected_event.text),
        )

        assert await driver.run_event(case.clean_event) == (
            await driver.run_event(case.injected_event)
        ), case.case_id
        assert injected.category is clean.category, case.case_id
        assert injected.proposed_action == clean.proposed_action, case.case_id
        # The injected sentence adds no term of the label's own category: at
        # most a milder category's, which a stricter label ignores.
        assert injected.terms == clean.terms, case.case_id


async def test_a_message_the_classifier_cannot_place_is_held(
    ledger: FrameworkServices,
):
    driver = MockTextEvalDriver(framework=ledger)

    for case in load_novel_pattern_cases(_NOVEL, event_type=CustomerMessage):
        for primer in case.priming_events:
            assert await driver.run_event(primer) == DecisionAction.ALLOW

        action = await driver.run_event(case.event)

        assert classify(case.event.text).category is UNCLASSIFIED, case.case_id
        assert action == DecisionAction.HOLD, case.case_id


async def test_a_forced_schema_failure_holds_without_a_gate_call_or_a_write(
    ledger: FrameworkServices, policy_gate: ScriptedPolicyGate
):
    driver = MockTextEvalDriver(framework=ledger)

    for case in load_schema_violation_cases(_MALFORMED, event_type=CustomerMessage):
        written = len(ledger.store.rows)
        asked = len(policy_gate.requests)

        action = await driver.run_with_forced_schema_failure(case.event)

        assert action == case.expected_action == DecisionAction.HOLD
        assert len(ledger.store.rows) == written
        assert len(policy_gate.requests) == asked


async def test_a_forced_fallback_holds_and_the_policy_double_is_not_asked(
    ledger: FrameworkServices,
    policy_gate: ScriptedPolicyGate,
    message: Callable[..., CustomerMessage],
):
    driver = MockTextEvalDriver(framework=ledger)

    for case in load_fallback_cases(_FALLBACK, event_type=CustomerMessage):
        for primer in case.priming_events:
            assert await driver.run_event(primer) == DecisionAction.ALLOW
        asked = len(policy_gate.requests)

        action = await driver.run_with_forced_fallback(
            case.event, fallback_kind=case.fallback_kind
        )

        assert action == case.expected_action == DecisionAction.HOLD
        assert len(policy_gate.requests) == asked
        # The message was gated, and the gate it reached failed closed.
        [bundle] = _bundles(ledger, case.event.event_id)
        assert bundle.submission.mode == "gate"
        record = bundle.gate("policy")
        assert record is not None
        assert record.status == "failed"

    # The swap lasted one call: the next message asks the double.
    asked = len(policy_gate.requests)
    after = message(1, "What are your opening hours on Saturday?")
    assert await driver.run_event(after) == DecisionAction.ALLOW
    assert len(policy_gate.requests) == asked + 1


async def test_run_reports_the_policy_verdict_of_its_gated_trigger(
    ledger: FrameworkServices,
    policy_gate: ScriptedPolicyGate,
    message: Callable[..., CustomerMessage],
):
    ordering = [
        message(1, "Please report fraud: there is a payment I did not make."),
        message(2, "Please change my address to 12 Elm Street."),
    ]

    result = await MockTextEvalDriver(framework=ledger).run(ordering)

    assert result == PipelineRunResult(
        decision_action=DecisionAction.CHALLENGE,
        confidence=0.9,
        rationale=SCRIPTED_RATIONALE,
    )
    # Every message is gated, precursors as well as the trigger.
    assert [r.submission.event_id for r in policy_gate.requests] == [
        "msg-test-0001",
        "msg-test-0002",
    ]


async def test_run_refuses_an_empty_ordering(ledger: FrameworkServices):
    with pytest.raises(ValueError, match="at least one event"):
        await MockTextEvalDriver(framework=ledger).run([])


async def test_reset_state_has_nothing_to_forget(
    ledger: FrameworkServices, message: Callable[..., CustomerMessage]
):
    driver = MockTextEvalDriver(framework=ledger)
    hardship = message(1, "I lost my job and can't pay this month's installment.")
    before = await driver.run_event(hardship)
    written = len(ledger.store.rows)

    driver.reset_state()

    assert len(ledger.store.rows) == written
    assert await driver.run_event(hardship) == before == DecisionAction.ALLOW
    first, second = (
        b.submission.reasoner_context for b in _bundles(ledger, hardship.event_id)
    )
    assert first == second


async def test_the_frameworks_dimensions_pass_over_the_shipped_datasets_in_memory(
    ledger: FrameworkServices, tmp_path: Path
):
    # The dimensions the harness builds for this adapter (eval.runners.harness),
    # on one driver over in-memory services instead of Postgres.
    driver = MockTextEvalDriver(framework=ledger)
    started = datetime.now(UTC)
    dimensions = [
        ConsistencyDimension(driver=driver, scenarios=_scenarios()),
        RobustnessDimension(
            driver=driver,
            injection_cases=load_injection_cases(
                _INJECTION, event_type=CustomerMessage
            ),
            schema_cases=load_schema_violation_cases(
                _MALFORMED, event_type=CustomerMessage
            ),
            novel_cases=load_novel_pattern_cases(_NOVEL, event_type=CustomerMessage),
            fallback_cases=load_fallback_cases(_FALLBACK, event_type=CustomerMessage),
        ),
        ShadowDimension(
            source=ledger.store,
            gates=ledger.gates,
            registry=ledger.registry,
            reasoner_id=ADAPTER.reasoner_id,
            since=started,
        ),
    ]

    report = await run_eval(
        dimensions=dimensions, output_path=tmp_path / "eval-report.json"
    )

    assert [
        (d.dimension, d.passed, d.threshold_violations, d.num_samples)
        for d in report.dimensions
    ] == [
        (EvalDimension.CONSISTENCY, True, [], 4),
        (EvalDimension.ROBUSTNESS, True, [], 11),
        (EvalDimension.SHADOW, True, [], 0),
    ]
    assert report.overall_passed
    assert report.consistency is not None
    assert report.consistency.action_stability_rate == 1.0
    assert report.robustness == RobustnessMetrics(
        injection_resistance_rate=1.0,
        schema_violation_handling=1.0,
        novel_pattern_action_accuracy=1.0,
        fallback_behavior_correctness=1.0,
    )
    # Every decision is gated, so nothing was recorded for the shadow to
    # re-decide.
    assert report.shadow is not None
    assert report.shadow.num_decisions == 0
    assert (tmp_path / "eval-report.json").is_file()
