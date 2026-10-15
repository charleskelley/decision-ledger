"""In-process driver into the example pipeline, for the eval harness.

Builds the ledger's services exactly as a deployment does
(``build_framework_services`` over the example plugin and every installed
gate) and submits through a ``LocalClient`` over them, so the eval runs the
same operation the HTTP route runs. Satisfies the harness's ``EvalDriver``
protocol structurally.

The example's scorer is stateless, so ``reset_state`` has nothing to do.
Fault injection is generic: the example does not reach into the policy
gate's internals, so every forced fallback swaps the policy gate for one
that raises, and the ledger fails it closed (DR-29) — the contract the
fallback dataset checks.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import TYPE_CHECKING, Any

from app.bootstrap import build_framework_services
from app.client import LocalClient
from app.enforcement.resolver import resolve_decision, resolve_gate_output
from app.gates import discover_gates
from core.bundle import GateRecord
from core.gate import GateInput, GateOutput
from eval.dimensions.consistency import PipelineRunResult
from example_reasoner.assembler import build_submission
from example_reasoner.pipeline import decide_widget
from example_reasoner.plugin import PLUGIN
from example_reasoner.registry import EXAMPLE_REGISTRATION
from example_reasoner.scorer import score_widget

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.gates import ComposedGate
    from app.plugin import FrameworkServices
    from app.settings import FrameworkSettings
    from core.actions import DecisionAction
    from core.bundle import DecisionBundle
    from core.gate_request import GateRequest
    from example_reasoner.events import WidgetEvent

#: The gate the example binds (advisory).
POLICY_GATE_ID = "policy"


class _FailingGate:
    """A gate that raises: the ledger fails it closed (fallback injection)."""

    def __init__(self, kind: str) -> None:
        self._kind = kind

    async def evaluate(self, request: GateRequest) -> GateOutput:
        msg = f"forced {self._kind} for the example's fallback dataset"
        raise RuntimeError(msg)

    def close(self) -> None:
        """Nothing to release."""


class ExampleEvalDriver:
    """The example pipeline against an in-process ledger.

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
        """Compose the ledger (or adopt ``framework``) and open a client."""
        if framework is None:
            from app.settings import FrameworkSettings

            framework = build_framework_services(
                settings if settings is not None else FrameworkSettings(),
                [PLUGIN],
                discover_gates(),
            )
        self._framework = framework
        self._client = LocalClient(framework)

    @property
    def framework(self) -> FrameworkServices:
        """The ledger services the pipeline runs on."""
        return self._framework

    async def _decide(
        self, event: WidgetEvent, framework: FrameworkServices | None = None
    ) -> DecisionBundle:
        client = (
            self._client
            if framework is None
            else self._client.with_framework(framework)
        )
        receipt = await decide_widget(event, client=client)
        return await client.get_bundle(receipt.decision_id)

    async def run(self, events: Sequence[WidgetEvent]) -> PipelineRunResult:
        """Decide every event; report the last (the trigger)."""
        bundle = None
        for event in events:
            bundle = await self._decide(event)
        if bundle is None:
            msg = "ExampleEvalDriver.run() requires at least one event"
            raise ValueError(msg)
        policy = bundle.gate(POLICY_GATE_ID)
        output = policy.output if policy is not None else None
        verdict = output.verdict if output is not None else None
        rationale = output.extra.get("rationale") if output is not None else None
        return PipelineRunResult(
            decision_action=bundle.decision_action,
            confidence=verdict.confidence if verdict is not None else None,
            rationale=rationale if isinstance(rationale, str) else None,
        )

    async def run_event(self, event: WidgetEvent) -> DecisionAction:
        """Decide one event through the normal pipeline."""
        return (await self._decide(event)).decision_action

    async def run_with_forced_schema_failure(
        self, event: WidgetEvent
    ) -> DecisionAction:
        """Enforce a gated decision whose policy gate returned no verdict.

        The submission is forced into gate mode (a widget in a band would be
        recorded and never reach the gate), and enforcement runs on a policy
        gate record with ``verdict=None`` — Tier 1 must hold it.
        """
        submission = build_submission(event, score_widget(event)).model_copy(
            update={"mode": "gate", "proposed_action": None, "bypass": None}
        )
        registration = self._framework.registry.get(EXAMPLE_REGISTRATION.reasoner_id)
        decision_id = str(uuid.uuid4())
        output = GateOutput(gate_id=POLICY_GATE_ID, deterministic=False, verdict=None)
        record = GateRecord(
            gate_id=POLICY_GATE_ID,
            mandatory=False,
            status="ran",
            input=GateInput(gate_id=POLICY_GATE_ID),
            output=output,
            enforcement=resolve_gate_output(
                output, registration=registration, decision_id=decision_id
            ),
        )
        decision = resolve_decision(
            submission=submission,
            records=[record],
            registration=registration,
            bypass_check=None,
            decision_id=decision_id,
        )
        return decision.decision_action

    async def run_with_forced_fallback(
        self, event: WidgetEvent, *, fallback_kind: str
    ) -> DecisionAction:
        """Decide one event with the policy gate failing (any ``fallback_kind``).

        Raises:
            RuntimeError: The deployment does not compose the policy gate.
        """
        composed: ComposedGate | None = self._framework.gates.get(POLICY_GATE_ID)
        if composed is None:
            msg = f"the {POLICY_GATE_ID!r} gate is not composed; nothing to fail"
            raise RuntimeError(msg)
        gates: dict[str, Any] = {
            **self._framework.gates,
            POLICY_GATE_ID: dataclasses.replace(
                composed, gate=_FailingGate(fallback_kind)
            ),
        }
        faulty = dataclasses.replace(self._framework, gates=gates)
        return (await self._decide(event, faulty)).decision_action

    def reset_state(self) -> None:
        """Nothing to reset: the example's scorer keeps no per-widget state."""

    def close(self) -> None:
        """Close the ledger services."""
        self._framework.close()
