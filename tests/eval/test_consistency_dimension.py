"""Unit tests for the consistency dimension.

Stubs the pipeline driver — a real driver going through ingestion → assembler
→ gate → enforcement → audit is integration-test territory. The unit tests
exercise:

- Pure metric helpers (is_stable, action_stability_rate, confidence variance)
- Dataset YAML loading
- Dimension wire-up: stability passes when all orderings agree, fails when
  any scenario has divergent decisions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from core.actions import DecisionAction
from core.eval.metrics import ConsistencyMetrics, EvalDimension
from eval.dimensions.consistency import (
    ConsistencyDimension,
    ConsistencyScenario,
    PipelineRunResult,
    ScenarioOrdering,
    action_stability_rate,
    confidence_variance_across_orderings,
    is_stable,
    load_scenarios,
)
from reasoner.account_takeover.events import (
    AuthMethod,
    AuthOutcome,
    Geolocation,
    LoginEvent,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_GEO = Geolocation(
    latitude=37.7749, longitude=-122.4194, country="US", city="SF", asn="AS15169"
)


def _event(event_id: str, account_id: str = "acct-1") -> LoginEvent:
    """Construct a minimal LoginEvent for ordering tests."""
    return LoginEvent(
        event_id=event_id,
        timestamp=datetime(2026, 4, 28, tzinfo=UTC),
        account_id=account_id,
        session_id=f"sess-{event_id}",
        ip_address="10.0.0.1",
        geolocation=_GEO,
        device_fingerprint="dev-1",
        user_agent="ua/1.0",
        auth_method=AuthMethod.PASSWORD,
        outcome=AuthOutcome.SUCCESS,
    )


class _StubDriver:
    """Pipeline driver that returns canned PipelineRunResults.

    Keyed by the trigger (last) event's event_id so a scenario can map
    different orderings to different outcomes when testing instability.
    """

    def __init__(self, by_trigger: dict[str, PipelineRunResult]) -> None:
        self._by_trigger = by_trigger
        self.calls: list[list[str]] = []

    async def run(self, events: Sequence[LoginEvent]) -> PipelineRunResult:
        ids = [e.event_id for e in events]
        self.calls.append(ids)
        trigger = ids[-1]
        return self._by_trigger[trigger]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestIsStable:
    def test_all_same(self) -> None:
        assert is_stable([DecisionAction.HOLD, DecisionAction.HOLD])

    def test_diverge(self) -> None:
        assert not is_stable([DecisionAction.HOLD, DecisionAction.BLOCK])

    def test_singleton(self) -> None:
        assert is_stable([DecisionAction.ALLOW])

    def test_empty(self) -> None:
        assert is_stable([])


class TestActionStabilityRate:
    def test_all_stable(self) -> None:
        rate = action_stability_rate(
            {
                "s1": [DecisionAction.HOLD, DecisionAction.HOLD],
                "s2": [DecisionAction.ALLOW, DecisionAction.ALLOW],
            }
        )
        assert rate == 1.0

    def test_one_unstable(self) -> None:
        rate = action_stability_rate(
            {
                "s1": [DecisionAction.HOLD, DecisionAction.BLOCK],
                "s2": [DecisionAction.ALLOW, DecisionAction.ALLOW],
            }
        )
        assert rate == 0.5

    def test_empty(self) -> None:
        assert action_stability_rate({}) == 0.0


class TestConfidenceVariance:
    def test_zero_when_constant(self) -> None:
        assert confidence_variance_across_orderings({"s1": [0.8, 0.8, 0.8]}) == 0.0

    def test_positive_when_diverse(self) -> None:
        assert confidence_variance_across_orderings({"s1": [0.6, 0.9]}) > 0.0

    def test_singleton_per_scenario_means_zero(self) -> None:
        # Variance of length-1 list is meaningless — treat as 0.
        assert confidence_variance_across_orderings({"s1": [0.7]}) == 0.0


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------


class TestLoadScenarios:
    def test_round_trip_yaml(self, tmp_path: Path) -> None:
        fixture = tmp_path / "scenarios.yaml"
        # Build YAML using the real LoginEvent JSON dump for exact-shape match.
        e1_json = _event("e1").model_dump_json()
        e2_json = _event("e2").model_dump_json()
        # YAML happens to accept JSON, so embedding the JSON dumps works.
        fixture.write_text(
            "scenarios:\n"
            "  - scenario_id: s1\n"
            "    description: ordering stability\n"
            "    orderings:\n"
            "      - ordering_id: chronological\n"
            f"        events: [{e1_json}, {e2_json}]\n"
            "      - ordering_id: reverse\n"
            f"        events: [{e2_json}, {e1_json}]\n",
            encoding="utf-8",
        )
        scenarios = load_scenarios(fixture)
        assert len(scenarios) == 1
        s = scenarios[0]
        assert s.scenario_id == "s1"
        assert len(s.orderings) == 2
        assert {o.ordering_id for o in s.orderings} == {"chronological", "reverse"}
        assert {e.event_id for e in s.orderings[0].events} == {"e1", "e2"}


# ---------------------------------------------------------------------------
# ConsistencyDimension
# ---------------------------------------------------------------------------


def _scenario_with_orderings(
    scenario_id: str,
    *,
    trigger_event_id: str = "trigger",
) -> ConsistencyScenario:
    """Construct a scenario where two orderings end on the same trigger event."""
    pre = _event("pre")
    trig = _event(trigger_event_id)
    return ConsistencyScenario(
        scenario_id=scenario_id,
        description="test",
        orderings=[
            ScenarioOrdering(ordering_id="forward", events=[pre, trig]),
            ScenarioOrdering(ordering_id="reverse", events=[pre, trig]),
        ],
    )


class TestConsistencyDimension:
    @pytest.mark.asyncio
    async def test_stable_run_passes(self) -> None:
        scenario = _scenario_with_orderings("s1")
        driver = _StubDriver(
            by_trigger={
                "trigger": PipelineRunResult(
                    decision_action=DecisionAction.HOLD,
                    confidence=0.7,
                ),
            }
        )
        dim = ConsistencyDimension(driver=driver, scenarios=[scenario])
        run = await dim.evaluate()

        assert run.result.dimension == EvalDimension.CONSISTENCY
        assert run.result.passed is True
        assert run.result.threshold_violations == []
        assert isinstance(run.metrics, ConsistencyMetrics)
        assert run.metrics.action_stability_rate == 1.0
        assert run.metrics.num_scenarios == 1
        assert run.metrics.num_orderings == 2
        # Driver was called once per ordering.
        assert len(driver.calls) == 2

    @pytest.mark.asyncio
    async def test_diverging_orderings_fail_zero_tolerance(self) -> None:
        # Two orderings of the SAME scenario produce different actions.
        # Trigger ID encodes the ordering so the stub returns different actions.
        pre = _event("pre")
        trig_a = _event("trig_a")
        trig_b = _event("trig_b")
        scenario = ConsistencyScenario(
            scenario_id="diverge",
            description="instability",
            orderings=[
                ScenarioOrdering(ordering_id="a", events=[pre, trig_a]),
                ScenarioOrdering(ordering_id="b", events=[pre, trig_b]),
            ],
        )
        driver = _StubDriver(
            by_trigger={
                "trig_a": PipelineRunResult(
                    decision_action=DecisionAction.HOLD,
                    confidence=0.7,
                ),
                "trig_b": PipelineRunResult(
                    decision_action=DecisionAction.BLOCK,
                    confidence=0.9,
                ),
            }
        )
        dim = ConsistencyDimension(driver=driver, scenarios=[scenario])
        run = await dim.evaluate()

        assert run.result.passed is False
        assert any("action_stability" in v for v in run.result.threshold_violations)
        # The unstable scenario id is named in the violation message.
        assert any("diverge" in v for v in run.result.threshold_violations)
        assert isinstance(run.metrics, ConsistencyMetrics)
        assert run.metrics.action_stability_rate == 0.0
        assert run.metrics.confidence_variance > 0.0

    @pytest.mark.asyncio
    async def test_mixed_stable_and_unstable_partial_score(self) -> None:
        stable = _scenario_with_orderings("stable", trigger_event_id="t_stable")
        pre = _event("pre")
        trig_a = _event("trig_a")
        trig_b = _event("trig_b")
        unstable = ConsistencyScenario(
            scenario_id="unstable",
            description="x",
            orderings=[
                ScenarioOrdering(ordering_id="a", events=[pre, trig_a]),
                ScenarioOrdering(ordering_id="b", events=[pre, trig_b]),
            ],
        )
        driver = _StubDriver(
            by_trigger={
                "t_stable": PipelineRunResult(
                    decision_action=DecisionAction.ALLOW, confidence=0.5
                ),
                "trig_a": PipelineRunResult(
                    decision_action=DecisionAction.HOLD, confidence=0.5
                ),
                "trig_b": PipelineRunResult(
                    decision_action=DecisionAction.BLOCK, confidence=0.5
                ),
            }
        )
        dim = ConsistencyDimension(driver=driver, scenarios=[stable, unstable])
        run = await dim.evaluate()

        assert run.result.passed is False
        assert isinstance(run.metrics, ConsistencyMetrics)
        # 1 of 2 stable → 0.5
        assert run.metrics.action_stability_rate == 0.5
