"""Dimension 03 — Decision Consistency.

The framework-novel dimension. Asserts that the same risk signals produce
the same final ``decision_action`` regardless of the temporal order in which
the precursor events arrived.

This is a property *only* this framework's design exposes — it depends on
``decision_action`` being immutable and on the bundle's replay contract. No
external eval library implements it because no external library knows about
``DecisionBundle``.

For each scenario:

1. Multiple orderings of the same precursor-event set are run through the
   pipeline.
2. The final (trigger) event in each ordering produces a ``decision_action``.
3. ``action_stability_rate`` = fraction of scenarios where all orderings
   agreed on the trigger's ``decision_action``. Zero tolerance.

``confidence_variance`` and ``rationale_semantic_similarity`` are recorded
for telemetry only — they are not gated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from statistics import variance
from typing import TYPE_CHECKING, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field

from core.actions import DecisionAction
from core.eval.metrics import (
    ConsistencyMetrics,
    DimensionResult,
    EvalDimension,
)
from eval.dimensions.base import DimensionRun
from reasoner.account_takeover.events import LoginEvent

if TYPE_CHECKING:
    from collections.abc import Sequence

# ---------------------------------------------------------------------------
# CI-gate thresholds
# ---------------------------------------------------------------------------

THRESHOLD_ACTION_STABILITY = 1.0  # zero tolerance

_DATASETS_DIR = Path(__file__).parents[1] / "datasets" / "scenarios"


# ---------------------------------------------------------------------------
# Scenario schema
# ---------------------------------------------------------------------------


class ScenarioOrdering(BaseModel):
    """One ordering of a scenario's events.

    Attributes:
        ordering_id: Stable identifier (e.g., ``"chronological"``,
            ``"reverse"``, ``"shuffled-1"``).
        events: Sequence of validated ``LoginEvent`` records. The last event
            is the *trigger* — its decision is what consistency compares
            across orderings.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    ordering_id: str
    events: list[LoginEvent] = Field(min_length=1)


class ConsistencyScenario(BaseModel):
    """A scenario with multiple orderings of the same event set.

    Attributes:
        scenario_id: Stable identifier.
        description: Human-readable description.
        orderings: At least 2 orderings to compare.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    scenario_id: str
    description: str
    orderings: list[ScenarioOrdering] = Field(min_length=2)


def load_scenarios(path: Path) -> list[ConsistencyScenario]:
    """Parse a YAML scenario file.

    Uses ``strict=False`` validation so YAML primitive strings (enum names,
    ISO timestamps) coerce into the strict-mode Pydantic models on disk.
    The on-disk YAML is the source of truth; the strict-mode runtime
    representation is only enforced after the dataset is in memory.

    Args:
        path: Path to the YAML file. Must contain a top-level ``scenarios:``
            list.

    Returns:
        Validated list of ``ConsistencyScenario``.
    """
    raw: dict = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        ConsistencyScenario.model_validate(s, strict=False) for s in raw["scenarios"]
    ]


# ---------------------------------------------------------------------------
# Pure metric helpers
# ---------------------------------------------------------------------------


def is_stable(actions: Sequence[DecisionAction]) -> bool:
    """All actions in the sequence are equal (vacuously true on length ≤ 1)."""
    if len(actions) <= 1:
        return True
    return all(a == actions[0] for a in actions[1:])


def action_stability_rate(scenario_actions: dict[str, list[DecisionAction]]) -> float:
    """Fraction of scenarios where every ordering produced the same action.

    Args:
        scenario_actions: ``scenario_id`` → list of decisions, one per ordering.

    Returns:
        Stability rate in ``[0, 1]``.
    """
    if not scenario_actions:
        return 0.0
    stable_count = sum(1 for actions in scenario_actions.values() if is_stable(actions))
    return stable_count / len(scenario_actions)


def confidence_variance_across_orderings(
    scenario_confidences: dict[str, list[float]],
) -> float:
    """Mean per-scenario variance in confidence across orderings.

    Telemetry only — not a CI gate. Tracks how much the gate's confidence
    fluctuates with event ordering.
    """
    if not scenario_confidences:
        return 0.0
    per_scenario = [
        variance(c) if len(c) >= 2 else 0.0 for c in scenario_confidences.values()
    ]
    return sum(per_scenario) / len(per_scenario)


# ---------------------------------------------------------------------------
# Pipeline driver protocol
# ---------------------------------------------------------------------------


class PipelineRunResult(BaseModel):
    """Output of a single pipeline run for one ordering.

    Attributes:
        decision_action: The pipeline's final action for the trigger event.
        confidence: Gate verdict confidence if the gate ran, else None.
        rationale: Gate verdict rationale if the gate ran, else None. Used
            only for telemetry (``rationale_semantic_similarity``); not
            gated.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    decision_action: DecisionAction
    confidence: float | None = None
    rationale: str | None = None


class PipelineDriver(Protocol):
    """SDK-agnostic driver into the live decision pipeline.

    The MVP implementation feeds events through the same path as
    ``app/main.py:create_decision`` (ingestion → assembler → gate →
    enforcement → audit) and returns the trigger's decision. Test stubs
    satisfy this same protocol so the dimension's wire-up is testable
    without infrastructure.
    """

    async def run(self, events: Sequence[LoginEvent]) -> PipelineRunResult:
        """Run the pipeline over an ordered event sequence.

        Args:
            events: Ordered events. The last event is the trigger; earlier
                events warm the feature window state.

        Returns:
            ``PipelineRunResult`` for the trigger event.
        """
        ...


# ---------------------------------------------------------------------------
# ConsistencyDimension
# ---------------------------------------------------------------------------


class ConsistencyDimension:
    """Drives scenario x ordering matrix; asserts trigger decisions are stable.

    Args:
        driver: ``PipelineDriver`` implementation.
        scenarios: Loaded ``ConsistencyScenario`` set.
    """

    kind = EvalDimension.CONSISTENCY

    def __init__(
        self,
        *,
        driver: PipelineDriver,
        scenarios: list[ConsistencyScenario],
    ) -> None:
        """Construct the dimension."""
        self._driver = driver
        self._scenarios = scenarios

    @classmethod
    def from_default_dataset(cls, *, driver: PipelineDriver) -> ConsistencyDimension:
        """Construct using the bundled scenario dataset."""
        scenarios: list[ConsistencyScenario] = []
        for path in sorted(_DATASETS_DIR.glob("*.yaml")):
            scenarios.extend(load_scenarios(path))
        return cls(driver=driver, scenarios=scenarios)

    async def evaluate(self) -> DimensionRun:
        """Run all scenarios x orderings and assemble the dimension run."""
        scenario_actions: dict[str, list[DecisionAction]] = {}
        scenario_confidences: dict[str, list[float]] = {}
        num_orderings_seen: list[int] = []

        for scenario in self._scenarios:
            actions: list[DecisionAction] = []
            confidences: list[float] = []
            for ordering in scenario.orderings:
                run = await self._driver.run(ordering.events)
                actions.append(run.decision_action)
                if run.confidence is not None:
                    confidences.append(run.confidence)
            scenario_actions[scenario.scenario_id] = actions
            scenario_confidences[scenario.scenario_id] = confidences
            num_orderings_seen.append(len(scenario.orderings))

        stability = action_stability_rate(scenario_actions)
        conf_var = confidence_variance_across_orderings(scenario_confidences)
        # rationale_semantic_similarity is currently a placeholder; computing
        # embedding similarity would re-introduce sentence-transformers in
        # eval. Telemetry-only, so 1.0 (no signal yet) is acceptable for MVP.
        rationale_sim = 1.0

        violations: list[str] = []
        if stability < THRESHOLD_ACTION_STABILITY:
            unstable = [
                sid for sid, acts in scenario_actions.items() if not is_stable(acts)
            ]
            violations.append(
                f"action_stability_rate {stability:.3f} < "
                f"{THRESHOLD_ACTION_STABILITY:.2f} "
                f"(zero tolerance) — unstable scenarios: {sorted(unstable)}"
            )

        n_scenarios = len(self._scenarios)
        n_orderings = num_orderings_seen[0] if num_orderings_seen else 0
        # If orderings vary across scenarios, report the minimum (conservative).
        if num_orderings_seen and len(set(num_orderings_seen)) > 1:
            n_orderings = min(num_orderings_seen)

        metrics = ConsistencyMetrics(
            num_scenarios=max(n_scenarios, 1),
            num_orderings=max(n_orderings, 1),
            action_stability_rate=round(stability, 4),
            confidence_variance=round(conf_var, 6),
            rationale_semantic_similarity=round(rationale_sim, 4),
        )
        result = DimensionResult(
            dimension=self.kind,
            passed=not violations,
            metrics={
                "action_stability_rate": metrics.action_stability_rate,
                "confidence_variance": metrics.confidence_variance,
                "rationale_semantic_similarity": metrics.rationale_semantic_similarity,
            },
            threshold_violations=violations,
            evaluated_at=datetime.now(UTC),
            num_samples=sum(num_orderings_seen),
        )
        return DimensionRun(result=result, metrics=metrics)


__all__ = [
    "THRESHOLD_ACTION_STABILITY",
    "ConsistencyDimension",
    "ConsistencyScenario",
    "PipelineDriver",
    "PipelineRunResult",
    "ScenarioOrdering",
    "action_stability_rate",
    "confidence_variance_across_orderings",
    "is_stable",
    "load_scenarios",
]
