"""The text mock's pipeline: classify, assemble, submit, and report the decision.

Run through ``LocalClient`` over the ledger in memory (the operation the
ledger's HTTP route runs), with the scripted policy double as the ledger's
policy gate:

- ``decide_message`` writes one gated bundle per message, under the
  registration's version, and raises the ledger's refusal;
- a message the double agrees with is decided as proposed, with no protocol
  hold;
- the planted disagreement: a hardship message proposes ALLOW, and the
  double, answering as the RAG policy gate does when its model reports that
  the regulatory text and the practice notes disagree, holds it for review
  with the regulatory corpus governing, citing the two planted passages. The
  hold is the gate's accepted verdict (no enforcement tier fired): Tier 5
  runs on its evidence and does not fire (the gate's reason says neither
  "conflict" nor "jurisdiction"), and it is not Tier 1's missing verdict;
- the proposal is a floor: a verdict permitting ALLOW lowers no message, and
  a stricter verdict raises one;
- a gate that raises fails closed: Tier 1 holds the message;
- every decision replays to the same action, and the report names an
  evidence source only for the scripted hold (the double retrieves
  nothing).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from app.auth import Principal
from app.client import LocalClient
from app.enforcement.resolver import TIER1_SCHEMA_FAILURE
from core.actions import DecisionAction
from core.exceptions import LedgerError
from core.observation import registration_version
from mock_text_reasoner.classifier import LABELS
from mock_text_reasoner.pipeline import decide_message, decision_report
from mock_text_reasoner.plugin import PLUGIN
from mock_text_reasoner.registry import MOCK_TEXT_REGISTRATION
from mock_text_reasoner.testing import (
    PROTOCOL_HOLD_REASON,
    SCRIPTED_HOLD_RATIONALE,
    SCRIPTED_RATIONALE,
    ScriptedPolicyGate,
    ScriptedPolicyGatePlugin,
)
from tests._memory_ledger import memory_framework

if TYPE_CHECKING:
    from app.plugin import FrameworkServices
    from core.gate import GateOutput
    from core.gate_request import GateRequest
    from mock_text_reasoner.messages import CustomerMessage

REASONER_ID = "mock-text-reasoner"


class _RaisingGate(ScriptedPolicyGate):
    """The double, admitting the binding as usual, then raising on a decision."""

    async def evaluate(self, request: GateRequest) -> GateOutput:
        self.requests.append(request)
        msg = "the test's policy gate fails"
        raise RuntimeError(msg)


def _ledger(gate: ScriptedPolicyGate) -> LocalClient:
    """A client over a ledger in memory whose policy gate is ``gate``."""
    return LocalClient(memory_framework([PLUGIN], [ScriptedPolicyGatePlugin(gate)]))


async def _report(message: CustomerMessage, client: LocalClient) -> dict[str, Any]:
    receipt, _ = await decide_message(message, client=client)
    return decision_report(await client.get_bundle(receipt.decision_id))


async def test_deciding_a_message_writes_one_gated_bundle(
    ledger: FrameworkServices,
    client: LocalClient,
    samples: dict[str, CustomerMessage],
):
    message = samples["general_inquiry"]

    receipt, submission = await decide_message(message, client=client)

    [bundle] = ledger.store.bundles()
    assert bundle.decision_id == receipt.decision_id
    assert bundle.submission == submission
    assert (receipt.reasoner_id, receipt.event_id, receipt.mode) == (
        REASONER_ID,
        message.event_id,
        "gate",
    )
    assert receipt.entity_id == str(message.entity_id)
    assert receipt.registration_version == registration_version(MOCK_TEXT_REGISTRATION)
    assert [(g.gate_id, g.status, g.action) for g in receipt.gates] == [
        ("policy", "ran", DecisionAction.ALLOW)
    ]


async def test_decide_message_raises_the_ledgers_refusal_and_writes_nothing(
    ledger: FrameworkServices, samples: dict[str, CustomerMessage]
):
    other = LocalClient(ledger, principal=Principal.for_reasoner("other-reasoner"))

    with pytest.raises(LedgerError) as refused:
        await decide_message(samples["general_inquiry"], client=other)

    assert (refused.value.status, refused.value.code) == (403, "REASONER_MISMATCH")
    assert ledger.store.rows == {}


async def test_a_message_the_double_agrees_with_is_decided_as_proposed(
    client: LocalClient, samples: dict[str, CustomerMessage]
):
    message = samples["general_inquiry"]

    report = await _report(message, client)

    assert report == {
        "decision_id": report["decision_id"],
        "event_id": message.event_id,
        "label": "general_inquiry",
        "proposed_action": "ALLOW",
        "decision_action": "ALLOW",
        "decision_level_rule": None,
        "enforcement_rule_applied": None,
        "policy": {
            "status": "ran",
            "permitted_actions": ["ALLOW"],
            "confidence": 0.9,
            "escalate_to_human": False,
            "conflict_detected": False,
            "evidence_sufficient": True,
            "governing_corpus_id": "mock-text-regulatory",
            "protocol_hold": None,
            "evidence_sources": [],
        },
    }
    assert json.loads(json.dumps(report)) == report


async def test_the_planted_disagreement_holds_a_hardship_message_for_review(
    holding_client: LocalClient,
    holding_gate: ScriptedPolicyGate,
    samples: dict[str, CustomerMessage],
):
    message = samples["hardship"]

    receipt, _ = await decide_message(message, client=holding_client)
    bundle = await holding_client.get_bundle(receipt.decision_id)
    report = decision_report(bundle)

    assert report == {
        "decision_id": receipt.decision_id,
        "event_id": message.event_id,
        "label": "hardship",
        "proposed_action": "ALLOW",
        "decision_action": "HOLD",
        "decision_level_rule": None,
        "enforcement_rule_applied": None,
        "policy": {
            "status": "ran",
            "permitted_actions": ["HOLD"],
            "confidence": 0.9,
            "escalate_to_human": True,
            "conflict_detected": True,
            "evidence_sufficient": True,
            "governing_corpus_id": "mock-text-regulatory",
            "protocol_hold": "conflict_detected",
            "evidence_sources": ["MT-PRAC-REPLIES", "MT-REG-ASSISTANT"],
        },
    }
    # The gate's held verdict was accepted as its output: no tier overrode it.
    # Tier 5 ran on the cited passages' jurisdiction and did not fire.
    record = bundle.gate("policy")
    assert record is not None
    assert record.enforcement is not None
    assert record.enforcement.enforcement_rule_applied is None
    assert "tier5_jurisdiction_conflict: not fired" in (record.enforcement.override_log)
    assert record.output is not None
    assert record.output.verdict is not None
    assert record.output.verdict.escalation_reason == PROTOCOL_HOLD_REASON
    assert record.output.extra["rationale"] == SCRIPTED_HOLD_RATIONALE
    assert "combinator: most_restrictive(proposed=ALLOW, advisory=HOLD) → HOLD" in (
        bundle.override_log
    )
    [request] = holding_gate.requests
    assert request.submission.reasoner_context.label_value == "hardship"


@pytest.mark.parametrize("label", [label for label in LABELS if label != "hardship"])
async def test_the_holding_double_holds_no_other_label(
    holding_client: LocalClient, samples: dict[str, CustomerMessage], label: str
):
    report = await _report(samples[label], holding_client)

    assert report["decision_action"] == LABELS[label].action.value
    assert report["policy"]["protocol_hold"] is None
    assert report["policy"]["conflict_detected"] is False


@pytest.mark.parametrize(
    ("label", "action"),
    [
        ("credential_request", DecisionAction.BLOCK),
        ("fraud_report", DecisionAction.HOLD),
        ("account_change", DecisionAction.CHALLENGE),
        ("hardship", DecisionAction.ALLOW),
        ("general_inquiry", DecisionAction.ALLOW),
        ("unclassified", DecisionAction.HOLD),
    ],
)
async def test_the_proposal_is_a_floor_under_a_verdict_permitting_allow(
    samples: dict[str, CustomerMessage], label: str, action: DecisionAction
):
    client = _ledger(ScriptedPolicyGate(action=DecisionAction.ALLOW))

    report = await _report(samples[label], client)

    assert report["policy"]["permitted_actions"] == ["ALLOW"]
    assert (report["proposed_action"], report["decision_action"]) == (
        action.value,
        action.value,
    )
    assert report["enforcement_rule_applied"] is None


async def test_a_verdict_stricter_than_the_proposal_raises_it(
    samples: dict[str, CustomerMessage],
):
    client = _ledger(ScriptedPolicyGate(action=DecisionAction.HOLD))

    report = await _report(samples["general_inquiry"], client)

    assert (report["proposed_action"], report["decision_action"]) == ("ALLOW", "HOLD")
    assert report["policy"]["permitted_actions"] == ["HOLD"]


async def test_a_gate_that_raises_fails_closed_and_tier_1_holds_the_message(
    samples: dict[str, CustomerMessage],
):
    gate = _RaisingGate()
    client = _ledger(gate)
    message = samples["general_inquiry"]

    receipt, _ = await decide_message(message, client=client)
    bundle = await client.get_bundle(receipt.decision_id)

    assert decision_report(bundle) == {
        "decision_id": receipt.decision_id,
        "event_id": message.event_id,
        "label": "general_inquiry",
        "proposed_action": "ALLOW",
        "decision_action": "HOLD",
        "decision_level_rule": None,
        "enforcement_rule_applied": TIER1_SCHEMA_FAILURE,
        "policy": {
            "status": "failed",
            "permitted_actions": None,
            "confidence": None,
            "escalate_to_human": None,
            "conflict_detected": None,
            "evidence_sufficient": None,
            "governing_corpus_id": None,
            "protocol_hold": None,
            "evidence_sources": [],
        },
    }
    record = bundle.gate("policy")
    assert record is not None
    assert record.reason is not None
    assert "RuntimeError" in record.reason
    assert len(gate.requests) == 1
    replay = await client.replay(receipt.decision_id)
    assert (replay.matched, replay.replayed_action) == (True, "HOLD")


async def test_every_decision_replays_to_the_same_action(
    client: LocalClient,
    holding_client: LocalClient,
    samples: dict[str, CustomerMessage],
):
    decided = [
        (client, (await decide_message(message, client=client))[0])
        for message in samples.values()
    ]
    held, _ = await decide_message(samples["hardship"], client=holding_client)
    decided.append((holding_client, held))

    for ledger_client, receipt in decided:
        replay = await ledger_client.replay(receipt.decision_id)

        assert replay.matched, receipt.event_id
        assert replay.actions_match
        assert replay.original_action == replay.replayed_action
        assert replay.original_action == receipt.decision_action.value
        assert replay.registration_version == receipt.registration_version
        assert [(g.gate_id, g.status) for g in replay.gates] == [("policy", "ran")]
    assert [receipt.decision_action for _, receipt in decided] == [
        *(LABELS[label].action for label in samples),
        DecisionAction.HOLD,
    ]


async def test_the_report_names_no_evidence_source_with_the_double(
    client: LocalClient, samples: dict[str, CustomerMessage]
):
    for label, message in samples.items():
        report = await _report(message, client)

        assert report["label"] == label
        assert report["policy"]["status"] == "ran"
        assert report["policy"]["evidence_sources"] == []
        assert report["policy"]["confidence"] == 0.9


async def test_the_double_explains_its_agreement_in_the_gates_output(
    client: LocalClient, samples: dict[str, CustomerMessage]
):
    receipt, _ = await decide_message(samples["account_change"], client=client)

    record = (await client.get_bundle(receipt.decision_id)).gate("policy")

    assert record is not None
    assert record.output is not None
    assert record.output.extra["rationale"] == SCRIPTED_RATIONALE
    assert record.output.extra["scripted"] is True
    assert record.output.evidence == ()
