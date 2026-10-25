"""In-process driver into the text mock's pipeline, for the eval harness.

Builds the ledger's services as a deployment does
(``build_framework_services``) over the mock's plugin and its scripted policy
double (``mock_text_reasoner.testing``), so ``make eval-mocks`` needs
Postgres but no Elasticsearch, corpus, or model, and submits through a
``LocalClient`` over them: the eval runs the operation the HTTP route runs.
Every message goes through the mock's own pipeline (``decide_message``: the
classifier, then the assembler). Satisfies the harness's ``EvalDriver``
protocol structurally.

The classifier keeps no state between messages, so ``reset_state`` has
nothing to forget and a consistency scenario's orderings differ only in the
order the ledger receives a customer's messages. ``run`` still resets at its
start, and every call holds a lock, as the other mocks' drivers do: the
harness runs its dimensions concurrently on one driver and never calls
``reset_state``, and the lock keeps each ordering's decisions together in
the ledger.

Fault injection is generic, as the example's is: the mock does not reach
into the policy gate, so a forced fallback swaps the composed gate for one
that raises, and the ledger fails it closed (DR-29).
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
from eval.dimensions.consistency import PipelineRunResult
from mock_text_reasoner.assembler import build_submission
from mock_text_reasoner.classifier import classify
from mock_text_reasoner.pipeline import decide_message
from mock_text_reasoner.plugin import PLUGIN
from mock_text_reasoner.registry import MOCK_TEXT_REGISTRATION
from mock_text_reasoner.testing import POLICY_GATE_ID, ScriptedPolicyGatePlugin

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.gates import ComposedGate
    from app.plugin import FrameworkServices
    from app.settings import FrameworkSettings
    from core.actions import DecisionAction
    from core.bundle import DecisionBundle
    from core.gate_request import GateRequest
    from mock_text_reasoner.messages import CustomerMessage


class _FailingGate:
    """A gate that raises: the ledger fails it closed (fallback injection)."""

    def __init__(self, kind: str) -> None:
        self._kind = kind

    async def evaluate(self, request: GateRequest) -> GateOutput:
        msg = f"forced {self._kind} for the text mock's fallback dataset"
        raise RuntimeError(msg)

    def close(self) -> None:
        """Nothing to release."""


class MockTextEvalDriver:
    """The text mock's pipeline against an in-process ledger.

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
                [ScriptedPolicyGatePlugin()],
            )
        self._framework = framework
        self._client = LocalClient(framework)
        self._lock = asyncio.Lock()

    @property
    def framework(self) -> FrameworkServices:
        """The ledger services the pipeline runs on."""
        return self._framework

    async def _decide(
        self, message: CustomerMessage, framework: FrameworkServices | None = None
    ) -> DecisionBundle:
        client = (
            self._client
            if framework is None
            else self._client.with_framework(framework)
        )
        receipt, _ = await decide_message(message, client=client)
        return await client.get_bundle(receipt.decision_id)

    async def run(self, events: Sequence[CustomerMessage]) -> PipelineRunResult:
        """Decide every message in order; report the last (the trigger)."""
        async with self._lock:
            self.reset_state()
            bundle = None
            for event in events:
                bundle = await self._decide(event)
        if bundle is None:
            msg = "MockTextEvalDriver.run() requires at least one event"
            raise ValueError(msg)
        policy = bundle.gate(POLICY_GATE_ID)
        output = policy.output if policy is not None else None
        verdict = output.verdict if output is not None else None
        rationale: Any = output.extra.get("rationale") if output is not None else None
        return PipelineRunResult(
            decision_action=bundle.decision_action,
            confidence=verdict.confidence if verdict is not None else None,
            rationale=rationale if isinstance(rationale, str) else None,
        )

    async def run_event(self, event: CustomerMessage) -> DecisionAction:
        """Decide one message through the normal pipeline."""
        async with self._lock:
            return (await self._decide(event)).decision_action

    async def run_with_forced_schema_failure(
        self, event: CustomerMessage
    ) -> DecisionAction:
        """Enforce a decision whose policy gate returned no verdict.

        The message is classified and assembled as usual (always gate mode,
        proposing the label's action), and enforcement runs on a policy gate
        record with ``verdict=None``: Tier 1 must hold it. Nothing is written
        and no gate runs.
        """
        async with self._lock:
            submission = build_submission(event, classify(event.text))
            registration = self._framework.registry.get(
                MOCK_TEXT_REGISTRATION.reasoner_id
            )
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
        self, event: CustomerMessage, *, fallback_kind: str
    ) -> DecisionAction:
        """Decide one message with the policy gate failing (any ``fallback_kind``).

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
        async with self._lock:
            return (await self._decide(event, faulty)).decision_action

    def reset_state(self) -> None:
        """Nothing to forget: the classifier keeps no state between messages."""

    def close(self) -> None:
        """Close the ledger services."""
        self._framework.close()
