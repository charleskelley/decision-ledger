"""The example's loop: score, assemble, submit through a ledger client.

Run through ``LocalClient`` over in-test framework services (the same
operation the ledger's HTTP route runs): a low widget is recorded ALLOW
without the gate, a mid widget is gated, a thin-history mid widget is held
for review (Tier 3), and a high widget is recorded BLOCK.
"""

from __future__ import annotations

from app.client import LocalClient
from core.actions import DecisionAction
from core.observation import registration_version
from example_reasoner.__main__ import sample_event
from example_reasoner.pipeline import decide_widget
from example_reasoner.registry import EXAMPLE_REGISTRATION


async def test_a_low_widget_is_recorded_without_the_gate(ledger, policy_gate):
    receipt = await decide_widget(sample_event("low"), client=LocalClient(ledger))

    assert receipt.mode == "record"
    assert receipt.decision_action == DecisionAction.ALLOW
    assert receipt.bypass_rule_id == "low-score"
    assert policy_gate.requests == []
    assert receipt.registration_version == registration_version(EXAMPLE_REGISTRATION)


async def test_a_mid_widget_is_decided_by_the_gate(ledger, policy_gate):
    client = LocalClient(ledger)
    receipt = await decide_widget(sample_event("mid"), client=client)

    assert receipt.mode == "gate"
    assert receipt.decision_action == DecisionAction.CHALLENGE
    [request] = policy_gate.requests
    assert request.gate_context.gate_config["template_id"] == "example-v1"
    bundle = await client.get_bundle(receipt.decision_id)
    assert bundle.gate("policy").output.extra["rationale"].startswith("Step-up")


async def test_a_thin_history_widget_is_held_for_review(ledger):
    event = sample_event("mid", prior_inspections=2)
    receipt = await decide_widget(event, client=LocalClient(ledger))

    assert receipt.decision_action == DecisionAction.HOLD
    assert receipt.decision_level_rule == "tier3_novel_entity"


async def test_a_high_widget_is_recorded_as_block(ledger, policy_gate):
    receipt = await decide_widget(sample_event("high"), client=LocalClient(ledger))

    assert receipt.decision_action == DecisionAction.BLOCK
    assert receipt.bypass_rule_id == "high-score"
    assert policy_gate.requests == []
