"""Fixtures for the example reasoner's tests: no infrastructure.

``ledger`` is framework services over an in-test store (it keeps written
bundles and answers lookups as the real store does) and a policy gate
double, so the example's pipeline and eval driver run through
``LocalClient`` — the same operation the ledger's HTTP route runs — without
Postgres, Elasticsearch, or an LLM.
"""

from __future__ import annotations

import json
from types import MappingProxyType
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.audit import StoredBundle
from app.gates import ComposedGate, GateKind
from app.plugin import FrameworkServices
from app.reasoner_registry import LiveReasonerRegistry
from core.actions import DecisionAction
from core.gate import GateOutput, GateVerdict
from example_reasoner.registry import EXAMPLE_REGISTRATION


class MemoryStore:
    """Keeps written bundles; answers ``lookup`` as the real store does."""

    def __init__(self) -> None:
        self.written: list[Any] = []

    def write(self, bundle: Any) -> None:
        self.written.append(bundle)

    def lookup(self, decision_id: str) -> StoredBundle:
        try:
            bundle = next(b for b in self.written if b.decision_id == decision_id)
        except StopIteration:
            raise KeyError(decision_id) from None
        return StoredBundle(
            decision_id=decision_id,
            reasoner_id=bundle.submission.reasoner_id,
            schema_version=2,
            payload=json.loads(bundle.model_dump_json()),
        )


class ScriptedPolicyGate:
    """A policy gate double: permits one action with a rationale."""

    def __init__(self, action: DecisionAction = DecisionAction.CHALLENGE) -> None:
        self.action = action
        self.requests: list[Any] = []

    async def evaluate(self, request: Any) -> GateOutput:
        self.requests.append(request)
        return GateOutput(
            gate_id="policy",
            deterministic=False,
            verdict=GateVerdict(
                gate_id="policy",
                permitted_actions=[self.action],
                required_controls=["sprocket_inspection"],
                confidence=0.9,
                escalate_to_human=False,
                escalation_reason=None,
            ),
            extra={"rationale": "Step-up inspection per EX-WIDGET-REL-A 3."},
        )

    def close(self) -> None:
        pass


class PolicyPlugin:
    gate_id = "policy"
    kind = GateKind.LLM
    deterministic = False
    config_model = None

    def build(self, framework: Any) -> Any:
        raise AssertionError("composed directly")


@pytest.fixture
def policy_gate() -> ScriptedPolicyGate:
    return ScriptedPolicyGate()


@pytest.fixture
def ledger(policy_gate: ScriptedPolicyGate) -> FrameworkServices:
    return FrameworkServices(
        settings=MagicMock(),
        pg_conn=MagicMock(),
        store=MemoryStore(),  # type: ignore[arg-type]
        registry=LiveReasonerRegistry([EXAMPLE_REGISTRATION]),
        gates=MappingProxyType(
            {"policy": ComposedGate(plugin=PolicyPlugin(), gate=policy_gate)}  # type: ignore[arg-type]
        ),
    )
