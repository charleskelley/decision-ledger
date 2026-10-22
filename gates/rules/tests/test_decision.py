"""One decision through the framework with the rules gate composed — no Docker.

Drives ``app.submit.submit`` with the rules gate built by its plugin, the way
a deployment composes it, and checks the framework-side contract end to end:
the gate config is validated at intake against ``RulesGateConfig``, the
gate's verdict decides the action, the deterministic flag skips enforcement
Tiers 2 and 4, the evidence (no jurisdictions) skips Tier 5, and the bundle
replays to the same action after the JSON round trip the store performs.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.audit import replay_bundle
from app.gates import ComposedGate
from app.reasoner_registry import LiveReasonerRegistry
from app.submit import submit
from core.actions import DecisionAction
from core.bundle import DecisionBundle
from core.exceptions import SubmissionContractError
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
    return {"rules": ComposedGate(plugin=plugin, gate=plugin.build(framework=None))}


async def _decide(submission, gates, registration, store):
    return await submit(
        submission=submission,
        gates=gates,
        store=store,  # type: ignore[arg-type]
        registry=LiveReasonerRegistry([registration]),
        ingestion_timestamp=datetime(2026, 10, 11, 12, 0, tzinfo=UTC),
    )


async def test_a_decision_through_the_rules_gate_is_enforced_and_replays(
    gates, registration, submission_for
):
    store = _RecordingStore()
    # Extreme score and enough wobble in REGION_A → BLOCK and CHALLENGE match;
    # the most conservative permitted action wins.
    submission = submission_for(label_value=0.97, region="REGION_A", wobble=9)

    bundle = await _decide(submission, gates, registration, store)

    assert store.written == [bundle]
    assert bundle.decision_action == DecisionAction.BLOCK
    assert bundle.decision_level_rule is None
    [record] = bundle.gates
    assert record.status == "ran"
    assert record.output is not None
    assert record.output.deterministic is True
    assert record.enforcement is not None
    assert record.enforcement.override_log == [
        "tier1_schema_failure: not fired — gate output present",
        "tier2_adversarial_probe: skipped — deterministic gate",
        "tier4_low_confidence: skipped — deterministic gate",
        "tier5_jurisdiction_conflict: skipped — gate evidence carries no jurisdictions",
        "tier6_default: accepted gate output — action=BLOCK",
    ]
    assert bundle.override_log == (
        "tier3_novel_entity: not fired — no novel-entity feature configured",
        "combinator: most_restrictive(advisory=BLOCK) → BLOCK",
    )

    reloaded = DecisionBundle.model_validate_json(bundle.model_dump_json())
    assert reloaded == bundle
    replayed = replay_bundle(reloaded, registration=registration)
    assert replayed.matched
    assert replayed.enforcement_decision.override_log == list(bundle.override_log)


async def test_an_unknown_ruleset_is_held_for_review(
    gates, registration, submission_for
):
    bundle = await _decide(
        submission_for(ruleset_id="no-such-set"),
        gates,
        registration,
        _RecordingStore(),
    )

    assert bundle.decision_action == DecisionAction.HOLD
    assert bundle.gates[0].enforcement.enforcement_rule_applied == (  # type: ignore[union-attr]
        "tier1_schema_failure"
    )


async def test_a_malformed_rules_gate_config_is_rejected_at_intake(
    gates, registration, submission_for
):
    store = _RecordingStore()
    submission = submission_for().model_copy(
        update={
            "gate_contexts": (GateContext(gate_id="rules", gate_config={"rules": []}),)
        }
    )

    with pytest.raises(SubmissionContractError, match="RulesGateConfig") as exc_info:
        await _decide(submission, gates, registration, store)

    assert exc_info.value.code == "GATE_CONFIG_INVALID"
    assert store.written == []
