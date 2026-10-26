"""In-process driver into the daily mock's pipeline, for the eval harness.

Builds the ledger's services as a deployment does
(``build_framework_services``) over the mock's plugin and the real rules gate
(``RulesGatePlugin`` over the member's own ``rulesets/``), so ``make
eval-mocks`` needs Postgres but no rule-set staging, Elasticsearch, or model,
and submits through a ``LocalClient`` over them: the eval runs the operations
the HTTP routes run. Every ordering is a day the mock runs as its CLI does
(``daily.run_day``): one decision per row, recorded within tolerance, gated
otherwise, and the rules gate, bound mandatory, on every one. Satisfies the
harness's ``EvalDriver`` protocol structurally.

The harness runs its dimensions concurrently on one driver and never calls
``reset_state``, so every call holds a lock and ``run`` resets the state at
the start of each ordering: the actions of the day's rows the driver has
decided. The computation reads each row alone, so nothing else carries from
one day to the next.

Fault injection is generic, as the other mocks' is: the mock does not reach
into the rules gate, so a forced fallback swaps the composed gate for one
that raises, and the ledger fails it closed (DR-29). The gate is mandatory,
so that holds a recorded row as well as an exception.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from typing import TYPE_CHECKING, Any

from app.bootstrap import build_framework_services
from app.client import LocalClient
from app.enforcement.resolver import resolve_decision, resolve_gate_output
from core.bundle import GateRecord
from core.gate import GateInput, GateOutput
from core.submission import evaluate_bypass, evaluate_competing_rules
from eval.dimensions.consistency import PipelineRunResult
from mock_daily_reasoner.assembler import RULES_GATE_ID, build_submission
from mock_daily_reasoner.compute import compute_drift
from mock_daily_reasoner.daily import run_day
from mock_daily_reasoner.plugin import PLUGIN, RULESETS_DIR
from mock_daily_reasoner.registry import MOCK_DAILY_REGISTRATION
from rules_gate import RulesGatePlugin
from rules_gate.settings import RulesGateSettings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.gates import ComposedGate
    from app.plugin import FrameworkServices
    from app.settings import FrameworkSettings
    from core.actions import DecisionAction
    from core.gate_request import GateRequest
    from mock_daily_reasoner.daily import DayRun
    from mock_daily_reasoner.settlements import AccountSettlement


def rules_gate_plugin() -> RulesGatePlugin:
    """The rules gate over the member's own rule set, read from no environment."""
    return RulesGatePlugin(RulesGateSettings(rulesets_dir=str(RULESETS_DIR)))


class _FailingGate:
    """A gate that raises: the ledger fails it closed (fallback injection)."""

    def __init__(self, kind: str) -> None:
        self._kind = kind

    async def evaluate(self, request: GateRequest) -> GateOutput:
        msg = f"forced {self._kind} for the daily mock's fallback dataset"
        raise RuntimeError(msg)

    def close(self) -> None:
        """Nothing to release."""


class MockDailyEvalDriver:
    """The daily mock's pipeline against an in-process ledger.

    Args:
        settings: Framework settings (Postgres).
        framework: Already-built services to run on instead (tests).
    """

    def __init__(
        self,
        settings: FrameworkSettings | None = None,
        *,
        framework: FrameworkServices | None = None,
    ) -> None:
        """Compose the ledger (or adopt ``framework``)."""
        if framework is None:
            from app.settings import FrameworkSettings

            framework = build_framework_services(
                settings if settings is not None else FrameworkSettings(),
                [PLUGIN],
                [rules_gate_plugin()],
            )
        self._framework = framework
        self._client = LocalClient(framework)
        self._decided: dict[str, DecisionAction] = {}
        self._lock = asyncio.Lock()

    @property
    def framework(self) -> FrameworkServices:
        """The ledger services the pipeline runs on."""
        return self._framework

    @property
    def decided(self) -> dict[str, DecisionAction]:
        """The action of every row decided since the state was last reset."""
        return dict(self._decided)

    async def _day(
        self,
        rows: Sequence[AccountSettlement],
        framework: FrameworkServices | None = None,
    ) -> DayRun:
        client = (
            self._client
            if framework is None
            else self._client.with_framework(framework)
        )
        run = await run_day(rows, client)
        for row in rows:
            action = run.action_of(row.event_id)
            if action is None:
                msg = f"the ledger did not decide {row.event_id!r}"
                raise RuntimeError(msg)
            self._decided[row.event_id] = action
        return run

    async def run(self, events: Sequence[AccountSettlement]) -> PipelineRunResult:
        """Run the rows as one day from fresh state; report the last (the trigger).

        The rules gate runs on every decision, so the trigger's result
        carries its verdict whether the row was recorded or gated.

        Raises:
            ValueError: No rows, or two rows share an ``event_id``.
            RuntimeError: The ledger did not decide a row.
        """
        if not events:
            msg = "MockDailyEvalDriver.run() requires at least one event"
            raise ValueError(msg)
        async with self._lock:
            self.reset_state()
            run = await self._day(events)
        trigger = events[-1].event_id
        outcome = next(d for d in run.decisions if d.event_id == trigger)
        if outcome.failure is not None:
            return PipelineRunResult(decision_action=outcome.decision_action)
        rules = ", ".join(outcome.matched_rules) or "the default action"
        return PipelineRunResult(
            decision_action=outcome.decision_action,
            confidence=1.0,
            rationale=f"rules gate: {rules}",
        )

    async def run_event(self, event: AccountSettlement) -> DecisionAction:
        """Decide one row as a day of its own (state kept)."""
        async with self._lock:
            await self._day([event])
            return self._decided[event.event_id]

    async def run_with_forced_schema_failure(
        self, event: AccountSettlement
    ) -> DecisionAction:
        """Enforce the row's decision with a rules gate that returned no verdict.

        The row is computed and assembled as the mock assembles it — recorded
        within tolerance, gated otherwise — and enforcement runs on a
        mandatory rules-gate record with ``verdict=None`` (and, for a record,
        the evaluation of the rule it cites): Tier 1 must hold it either way.
        Nothing is written and no gate runs.
        """
        async with self._lock:
            submission = build_submission(event, compute_drift(event))
            registration = self._framework.registry.get(
                MOCK_DAILY_REGISTRATION.reasoner_id
            )
            context = submission.reasoner_context
            cited = (
                registration.bypass_rule(submission.bypass.rule_id)
                if submission.bypass is not None
                else None
            )
            decision_id = str(uuid.uuid4())
            output = GateOutput(gate_id=RULES_GATE_ID, deterministic=True, verdict=None)
            record = GateRecord(
                gate_id=RULES_GATE_ID,
                mandatory=True,
                status="ran",
                input=GateInput(gate_id=RULES_GATE_ID),
                output=output,
                enforcement=resolve_gate_output(
                    output, registration=registration, decision_id=decision_id
                ),
            )
            decision = resolve_decision(
                submission=submission,
                records=[record],
                registration=registration,
                bypass_check=(
                    evaluate_bypass(cited, context) if cited is not None else None
                ),
                decision_id=decision_id,
                competing_checks=(
                    evaluate_competing_rules(registration, cited, context)
                    if cited is not None
                    else ()
                ),
            )
            return decision.decision_action

    async def run_with_forced_fallback(
        self, event: AccountSettlement, *, fallback_kind: str
    ) -> DecisionAction:
        """Decide one row with the rules gate failing (any ``fallback_kind``).

        The gate is mandatory, so it is invoked, and fails closed, whether
        the row is recorded or gated.

        Raises:
            RuntimeError: The deployment does not compose the rules gate.
        """
        composed: ComposedGate | None = self._framework.gates.get(RULES_GATE_ID)
        if composed is None:
            msg = f"the {RULES_GATE_ID!r} gate is not composed; nothing to fail"
            raise RuntimeError(msg)
        gates: dict[str, Any] = {
            **self._framework.gates,
            RULES_GATE_ID: dataclasses.replace(
                composed, gate=_FailingGate(fallback_kind)
            ),
        }
        faulty = dataclasses.replace(self._framework, gates=gates)
        async with self._lock:
            await self._day([event], faulty)
            return self._decided[event.event_id]

    def reset_state(self) -> None:
        """Forget the actions of the rows decided so far."""
        self._decided.clear()

    def close(self) -> None:
        """Close the ledger services."""
        self._framework.close()
