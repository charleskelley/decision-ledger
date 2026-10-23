"""The stream mock's loop: read the account's window, score, assemble, submit.

Run through ``LocalClient`` over the ledger in memory (the operation the
ledger's HTTP route runs), with the scripted policy double as the ledger's
policy gate:

- a steady account's transfers are recorded ALLOW under ``steady-activity``
  and never reach the gate;
- a fresh account's large first transfer is gated and held by Tier 3
  (``tier3_novel_entity``) although the gate gave its verdict;
- an established account's transfer six times its baseline is gated and
  decided by the gate (CHALLENGE, no Tier 3);
- a burst of transfers escalates from recorded ALLOW through the gate to
  recorded BLOCK under ``activity-surge``;
- every receipt carries the registration's version, and a gated decision
  replays to the same action.

``StreamProcessor`` remembers and counts each decision, and dead-letters
what the ledger refuses (``rejected``), cannot be reached for
(``unreachable``), or what fails on the way (``failed``), learning the
transfer either way; one bad transfer never stops the stream, and ``run``
decides everything published and returns once the source closes.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx
import pytest

from app.client import LocalClient
from core.actions import DecisionAction
from core.exceptions import LedgerError
from core.observation import registration_version
from mock_stream_reasoner.pipeline import StreamProcessor, decide_transfer
from mock_stream_reasoner.registry import MOCK_STREAM_REGISTRATION
from mock_stream_reasoner.source import EventSource
from mock_stream_reasoner.state import AccountWindows
from mock_stream_reasoner.testing import SCRIPTED_RATIONALE

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.api.models import DecisionReceipt
    from app.client import LedgerClient
    from app.plugin import FrameworkServices
    from core.submission import Submission
    from mock_stream_reasoner.events import TransferEvent
    from mock_stream_reasoner.testing import ScriptedPolicyGate


class _ScriptedLedger:
    """A ledger client double: fails the scripted event ids, passes the rest on."""

    def __init__(
        self, failures: dict[str, Exception], inner: LedgerClient | None = None
    ) -> None:
        self.failures = failures
        self.inner = inner
        self.submitted: list[str] = []

    async def submit(self, submission: Submission) -> DecisionReceipt:
        self.submitted.append(submission.event_id)
        error = self.failures.get(submission.event_id)
        if error is not None:
            raise error
        assert self.inner is not None, "the double has no ledger behind it"
        return await self.inner.submit(submission)


async def _decide_all(
    events: list[TransferEvent], client: LedgerClient
) -> list[tuple[DecisionReceipt, bool]]:
    windows = AccountWindows()
    return [
        await decide_transfer(event, windows=windows, client=client) for event in events
    ]


async def test_a_steady_accounts_transfers_are_recorded_allow_without_the_gate(
    ledger: FrameworkServices,
    policy_gate: ScriptedPolicyGate,
    transfer: Callable[..., TransferEvent],
):
    events = [transfer(n, "acct-steady", 100.0, minutes=15 * n) for n in range(4)]

    decided = await _decide_all(events, LocalClient(ledger))

    receipts = [receipt for receipt, _ in decided]
    assert {r.mode for r in receipts} == {"record"}
    assert {r.decision_action for r in receipts} == {DecisionAction.ALLOW}
    assert {r.bypass_rule_id for r in receipts} == {"steady-activity"}
    assert all(r.bypass_rule_holds for r in receipts)
    assert {r.decision_level_rule for r in receipts} == {None}
    assert [thin for _, thin in decided] == [True, True, True, False]
    assert {(g.gate_id, g.status) for r in receipts for g in r.gates} == {
        ("policy", "skipped")
    }
    assert policy_gate.requests == []


async def test_a_fresh_accounts_large_first_transfer_is_held_by_tier3(
    ledger: FrameworkServices,
    policy_gate: ScriptedPolicyGate,
    transfer: Callable[..., TransferEvent],
):
    [(receipt, thin)] = await _decide_all(
        [transfer(1, "acct-fresh", 450.0)], LocalClient(ledger)
    )

    assert thin
    assert receipt.mode == "gate"
    assert receipt.decision_action == DecisionAction.HOLD
    assert receipt.decision_level_rule == "tier3_novel_entity"
    assert receipt.bypass_rule_id is None
    [gate] = receipt.gates
    assert (gate.gate_id, gate.status, gate.action) == (
        "policy",
        "ran",
        DecisionAction.CHALLENGE,
    )
    [request] = policy_gate.requests
    assert [c["corpus_id"] for c in request.binding_config["corpora"]] == [
        "mock-stream-practices"
    ]
    assert request.binding_config["corpora"][0]["tier"] == 2
    config = request.gate_context.gate_config
    assert config["template_id"] == "mock-stream-v1"
    assert config["jurisdictions"] == ["GLOBAL"]
    assert config["template_vars"]["thin_history"] == "True"


async def test_an_established_accounts_sixfold_transfer_is_decided_by_the_gate(
    ledger: FrameworkServices,
    policy_gate: ScriptedPolicyGate,
    transfer: Callable[..., TransferEvent],
):
    client = LocalClient(ledger)
    history = [transfer(n, "acct-established", 100.0, minutes=15 * n) for n in range(3)]
    large = transfer(3, "acct-established", 600.0, minutes=45)

    *_, (receipt, thin) = await _decide_all([*history, large], client)

    assert not thin
    assert receipt.mode == "gate"
    assert receipt.decision_action == DecisionAction.CHALLENGE
    assert receipt.decision_level_rule is None
    [request] = policy_gate.requests
    assert request.submission.event_id == large.event_id
    bundle = await client.get_bundle(receipt.decision_id)
    policy = bundle.gate("policy")
    assert policy is not None
    assert policy.output is not None
    assert policy.output.extra["rationale"] == SCRIPTED_RATIONALE


async def test_a_burst_escalates_to_a_recorded_block_under_activity_surge(
    ledger: FrameworkServices,
    policy_gate: ScriptedPolicyGate,
    transfer: Callable[..., TransferEvent],
):
    warm_up = [transfer(n, "acct-burst", 100.0, minutes=15 * n) for n in range(3)]
    burst = [
        transfer(10 + n, "acct-burst", 250.0, minutes=60 + n * 40 / 60)
        for n in range(9)
    ]

    decided = await _decide_all([*warm_up, *burst], LocalClient(ledger))

    outcomes = [
        (r.mode, r.decision_action.value, r.bypass_rule_id) for r, _ in decided[3:]
    ]
    allow = ("record", "ALLOW", "steady-activity")
    gated = ("gate", "CHALLENGE", None)
    block = ("record", "BLOCK", "activity-surge")
    assert outcomes == [allow] * 2 + [gated] * 4 + [block] * 3
    assert len(policy_gate.requests) == 4


async def test_every_receipt_carries_the_registrations_version(
    ledger: FrameworkServices, transfer: Callable[..., TransferEvent]
):
    decided = await _decide_all(
        [transfer(1, "acct-steady", 100.0), transfer(2, "acct-fresh", 450.0)],
        LocalClient(ledger),
    )

    assert [r.mode for r, _ in decided] == ["record", "gate"]
    assert {r.registration_version for r, _ in decided} == {
        registration_version(MOCK_STREAM_REGISTRATION)
    }


async def test_a_gated_decision_replays_to_the_same_action(
    ledger: FrameworkServices, transfer: Callable[..., TransferEvent]
):
    client = LocalClient(ledger)
    history = [transfer(n, "acct-established", 100.0, minutes=15 * n) for n in range(3)]
    decided = await _decide_all(
        [
            transfer(9, "acct-fresh", 450.0),
            *history,
            transfer(3, "acct-established", 600.0, minutes=45),
        ],
        client,
    )
    held, challenged = decided[0][0], decided[-1][0]

    for receipt in (held, challenged):
        replay = await client.replay(receipt.decision_id)
        assert replay.matched
        assert replay.replayed_action == replay.original_action
        assert replay.original_action == receipt.decision_action.value
        assert replay.registration_version == receipt.registration_version
    assert (held.decision_action, challenged.decision_action) == (
        DecisionAction.HOLD,
        DecisionAction.CHALLENGE,
    )


async def test_the_processor_remembers_and_counts_each_decision(
    ledger: FrameworkServices, transfer: Callable[..., TransferEvent]
):
    source = EventSource()
    processor = StreamProcessor(LocalClient(ledger))

    steady = await processor.process(transfer(1, "acct-steady", 100.0), source)
    held = await processor.process(transfer(2, "acct-fresh", 450.0), source)

    assert steady is not None
    assert held is not None
    assert (
        steady.event_id,
        steady.account_id,
        steady.mode,
        steady.decision_action,
        steady.bypass_rule_id,
        steady.decision_level_rule,
        steady.thin_history,
    ) == (
        "evt-test-0001",
        "acct-steady",
        "record",
        "ALLOW",
        "steady-activity",
        None,
        True,
    )
    assert (
        held.event_id,
        held.mode,
        held.decision_action,
        held.bypass_rule_id,
        held.decision_level_rule,
        held.thin_history,
    ) == ("evt-test-0002", "gate", "HOLD", None, "tier3_novel_entity", True)
    assert steady.decision_id != held.decision_id
    assert processor.decisions == (steady, held)
    assert processor.stats() == {
        "action_ALLOW": 1,
        "action_HOLD": 1,
        "decided": 2,
        "mode_gate": 1,
        "mode_record": 1,
        "rule_tier3_novel_entity": 1,
    }
    assert source.dead_letters == ()


@pytest.mark.parametrize(
    ("error", "reason", "detail"),
    [
        pytest.param(
            LedgerError(422, "NATIVE_SUBJECT_MISMATCH", "the subject disagrees"),
            "rejected",
            "422 NATIVE_SUBJECT_MISMATCH: the subject disagrees",
            id="refused",
        ),
        pytest.param(
            LedgerError(503, "SERVICE_UNAVAILABLE", "the ledger is starting"),
            "unreachable",
            "503 SERVICE_UNAVAILABLE: the ledger is starting",
            id="server-error",
        ),
        pytest.param(
            httpx.ConnectError("connection refused"),
            "unreachable",
            "the ledger could not be reached (ConnectError)",
            id="connect-error",
        ),
        pytest.param(
            RuntimeError("something broke"),
            "failed",
            "the decision failed (RuntimeError)",
            id="unexpected-error",
        ),
    ],
)
async def test_a_transfer_the_ledger_does_not_decide_is_dead_lettered_and_learned(
    error: Exception,
    reason: str,
    detail: str,
    transfer: Callable[..., TransferEvent],
):
    client = _ScriptedLedger({"evt-test-0001": error})
    source = EventSource()
    processor = StreamProcessor(client)
    event = transfer(1, "acct-a", 100.0)

    assert await processor.process(event, source) is None

    [letter] = source.dead_letters
    assert (letter.reason, letter.event_id, letter.detail) == (
        reason,
        "evt-test-0001",
        detail,
    )
    assert letter.payload == event.payload()
    assert source.stats()[f"dead_lettered_{reason}"] == 1
    assert client.submitted == ["evt-test-0001"]
    assert processor.decisions == ()
    assert processor.stats() == {"dead_lettered": 1}
    # The transfer happened whatever the ledger answered: the state learned it.
    assert len(processor.windows) == 1
    after = processor.windows.observe(transfer(2, "acct-a", 100.0, minutes=1))
    assert (after.prior_events, after.window_events) == (1, 1)


async def test_one_refused_transfer_does_not_stop_the_stream(
    ledger: FrameworkServices, transfer: Callable[..., TransferEvent]
):
    refusal = LedgerError(422, "NATIVE_SUBJECT_MISMATCH", "the subject disagrees")
    client = _ScriptedLedger({"evt-test-0001": refusal}, inner=LocalClient(ledger))
    source = EventSource()
    processor = StreamProcessor(client)
    for n in range(4):
        await source.publish(transfer(n, "acct-a", 100.0, minutes=15 * n).payload())
    source.close()

    await asyncio.wait_for(processor.run(source), timeout=10)

    assert client.submitted == [f"evt-test-{n:04d}" for n in range(4)]
    assert [(d.event_id, d.thin_history) for d in processor.decisions] == [
        ("evt-test-0000", True),
        ("evt-test-0002", True),
        # The refused transfer still counts toward the account's history.
        ("evt-test-0003", False),
    ]
    assert [(letter.reason, letter.event_id) for letter in source.dead_letters] == [
        ("rejected", "evt-test-0001")
    ]
    assert processor.stats()["decided"] == 3
    assert processor.stats()["dead_lettered"] == 1


async def test_run_decides_everything_published_and_returns_once_the_source_closes(
    ledger: FrameworkServices, transfer: Callable[..., TransferEvent]
):
    source = EventSource()
    processor = StreamProcessor(LocalClient(ledger))
    consumer = asyncio.create_task(processor.run(source))
    published = [
        transfer(n, f"acct-{n % 2}", 100.0, minutes=15 * n).payload() for n in range(5)
    ]

    for payload in published:
        assert (await source.publish(payload)).outcome == "accepted"
    await asyncio.wait_for(source.drained(), timeout=10)

    assert not consumer.done()
    assert [d.event_id for d in processor.decisions] == [
        p["event_id"] for p in published
    ]
    source.close()
    await asyncio.wait_for(consumer, timeout=10)
    assert consumer.exception() is None
    assert source.pending == 0
    assert processor.stats()["decided"] == 5
