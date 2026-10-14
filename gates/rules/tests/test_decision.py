"""One decision through the framework with the rules gate composed — no Docker.

Drives ``app.decide.execute_decision`` with the rules gate built by its
plugin, the way a deployment composes it, and checks the framework-side
contract end to end: the gate config is validated at intake against
``RulesGateConfig``, the gate's verdict decides the action, the deterministic
flag skips enforcement Tiers 2 and 4, the evidence (no jurisdictions) skips
Tier 5, and the stored gate output replays to the same action after the JSON
round trip the bundle store performs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.decide import execute_decision
from app.enforcement.resolver import resolve
from app.gates import ComposedGate
from app.reasoner_registry import StaticReasonerRegistry
from core.actions import DecisionAction
from core.exceptions import ObservationContractError
from core.gate import GateOutput
from core.observation import GateContext
from rules_gate import RulesGatePlugin
from rules_gate.settings import RulesGateSettings


class _RecordingStore:
    def __init__(self) -> None:
        self.written: list = []

    def write(self, bundle) -> None:
        self.written.append(bundle)


@pytest.fixture
def gates(write_ruleset) -> dict[str, ComposedGate]:
    plugin = RulesGatePlugin(RulesGateSettings(rulesets_dir=str(write_ruleset())))
    return {"rules": ComposedGate(plugin=plugin, gate=plugin.build(framework=None))}  # type: ignore[arg-type]


async def _decide(observation, gates, registration, store):
    return await execute_decision(
        observation=observation,
        gates=gates,
        store=store,  # type: ignore[arg-type]
        registry=StaticReasonerRegistry([registration]),
        ingestion_timestamp=datetime(2026, 10, 11, 12, 0, tzinfo=UTC),
    )


async def test_a_decision_through_the_rules_gate_is_enforced_and_replays(
    gates, registration, observation_for
):
    store = _RecordingStore()
    # Extreme score and enough wobble in REGION_A → BLOCK and CHALLENGE match;
    # the most conservative permitted action wins.
    observation = observation_for(label_value=0.97, region="REGION_A", wobble=9)

    bundle = await _decide(observation, gates, registration, store)

    assert store.written == [bundle]
    assert bundle.decision_action == DecisionAction.BLOCK
    assert bundle.enforcement_rule_applied is None
    assert bundle.gate_output is not None
    assert bundle.gate_output.deterministic is True
    assert bundle.override_log == [
        "tier1_schema_failure: not fired — gate output present",
        "tier2_adversarial_probe: skipped — deterministic gate",
        "tier4_low_confidence: skipped — deterministic gate",
        "tier5_jurisdiction_conflict: skipped — gate evidence carries no jurisdictions",
        "tier6_default: accepted gate output — action=BLOCK",
        "tier3_novel_entity: not fired — no novel-entity feature configured",
    ]

    stored = json.loads(json.dumps(bundle.gate_output.model_dump(mode="json")))
    replayed = resolve(
        bundle.raw_event,
        GateOutput.model_validate(stored, strict=False),
        decision_id=bundle.decision_id,
        registration=registration,
    )
    assert replayed.decision_action == bundle.decision_action
    assert replayed.override_log == bundle.override_log


async def test_an_unknown_ruleset_is_held_for_review(
    gates, registration, observation_for
):
    bundle = await _decide(
        observation_for(ruleset_id="no-such-set"),
        gates,
        registration,
        _RecordingStore(),
    )

    assert bundle.decision_action == DecisionAction.HOLD
    assert bundle.enforcement_rule_applied == "tier1_schema_failure"


async def test_a_malformed_rules_gate_config_is_rejected_at_intake(
    gates, registration, observation_for
):
    store = _RecordingStore()
    observation = observation_for().model_copy(
        update={"gate_context": GateContext(gate_id="rules", gate_config={"rules": []})}
    )

    with pytest.raises(ObservationContractError, match="RulesGateConfig"):
        await _decide(observation, gates, registration, store)

    assert store.written == []
