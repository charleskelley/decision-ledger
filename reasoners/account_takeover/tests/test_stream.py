"""The stream adapter and its CLI: accepted logins, decided in stream order.

``decide_from_stream`` joins the synchronous ``IngestionConsumer`` to the
async ``run_ato_decision``. These tests drive it with an in-memory source
(the consumer's four methods, over a list of messages), ATO's services on
stubs, and a ledger client that records submissions or refuses them, so the
delivery contract is pinned without Redis: stream order, the ``max_events``
bound, dead-lettering when a decision fails, and stopping. The ``consume``
command runs the same adapter over the same doubles. The consumer's own
checks against Redis, and a run into a Postgres ledger, are integration
tests.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

import ato_reasoner.__main__ as cli
from app.api.models import DecisionReceipt
from ato_reasoner.events import LoginEvent
from ato_reasoner.ingestion.stream import (
    LEDGER_REFUSED,
    PIPELINE_ERROR,
    StreamRun,
    decide_from_stream,
)
from ato_reasoner.plugin import AtoServices
from ato_reasoner.scorer import ScorerOutput
from core.actions import DecisionAction
from core.exceptions import LedgerError

if TYPE_CHECKING:
    from core.submission import Submission


class FakeSource:
    """An in-memory stream consumer with the real one's four methods.

    Like a Redis consumer group: reading new messages delivers them to this
    consumer, where they stay pending until ``process_message`` ACKs them;
    a ``pending`` read re-reads that backlog, oldest first.
    """

    def __init__(self, messages: list[tuple[str, str]]) -> None:
        self.unread = list(messages)
        self.pending: list[tuple[str, str]] = []
        self.reads: list[tuple[int, bool]] = []
        self.checked: list[str] = []
        self.dead_letters: list[dict[str, str | None]] = []
        self.groups_ensured = 0

    @property
    def new_reads(self) -> list[int]:
        """The batch sizes asked of new-message reads."""
        return [size for size, pending in self.reads if not pending]

    def ensure_group(self) -> None:
        self.groups_ensured += 1

    def read(
        self, *, batch_size: int, block_ms: int, pending: bool = False
    ) -> list[tuple[str, str]]:
        self.reads.append((batch_size, pending))
        if pending:
            return self.pending[:batch_size]
        batch, self.unread = self.unread[:batch_size], self.unread[batch_size:]
        self.pending.extend(batch)
        return batch

    def process_message(self, message_id: str, raw_data: str) -> LoginEvent | None:
        self.checked.append(message_id)
        self.pending = [m for m in self.pending if m[0] != message_id]  # the ACK
        try:
            return LoginEvent.model_validate_json(raw_data)
        except ValueError:
            self.dead_letter(
                message_id, raw_data, reason="schema_error", detail="not a login"
            )
            return None

    def dead_letter(
        self,
        message_id: str,
        raw_data: str,
        *,
        reason: str,
        detail: str,
        event_id: str | None = None,
    ) -> None:
        self.dead_letters.append(
            {
                "original_id": message_id,
                "data": raw_data,
                "reason": reason,
                "detail": detail,
                "event_id": event_id,
            }
        )


class ScriptedClient:
    """Records what ATO submits; raises the error scripted for an event id."""

    def __init__(self, errors: dict[str, Exception] | None = None) -> None:
        self.errors = errors or {}
        self.submitted: list[Submission] = []
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True

    async def submit(self, submission: Submission) -> DecisionReceipt:
        error = self.errors.get(submission.event_id)
        if error is not None:
            raise error
        self.submitted.append(submission)
        return DecisionReceipt(
            decision_id=str(uuid4()),
            reasoner_id=submission.reasoner_id,
            event_id=submission.event_id,
            entity_id=str(submission.entity_id),
            mode=submission.mode,
            decision_action=submission.proposed_action or DecisionAction.HOLD,
            decision_level_rule=None,
            enforcement_rule_applied=None,
            bypass_rule_id=submission.bypass.rule_id if submission.bypass else None,
            bypass_rule_holds=True if submission.bypass else None,
            registration_version="v",
            gates=[],
            latency_breakdown={},
            created_at=datetime.now(UTC),
        )


@pytest.fixture
def services(ato_feature_vector) -> AtoServices:
    """ATO's services on stubs: every login scores 0.05 (recorded ALLOW)."""
    feature_svc = MagicMock()
    feature_svc.compute.return_value = ato_feature_vector
    scorer = MagicMock()
    scorer.score.return_value = ScorerOutput(
        entity_id=uuid4(),
        risk_score=0.05,
        top_signals=[],
        scorer_version="xgb-test",
        inference_latency_ms=1.0,
    )
    return AtoServices(
        feature_svc=feature_svc, scorer=scorer, template_id="ato-v4", redis=MagicMock()
    )


@pytest.fixture
def stream(login_event: LoginEvent):
    """``n`` stream messages, each a login with its own event id."""

    def _stream(n: int) -> list[tuple[str, str]]:
        return [
            (
                f"{i}-0",
                login_event.model_copy(
                    update={"event_id": f"evt-stream-{i}"}
                ).model_dump_json(),
            )
            for i in range(1, n + 1)
        ]

    return _stream


async def test_every_accepted_login_is_decided_in_stream_order(services, stream):
    source = FakeSource(stream(3))
    client = ScriptedClient()
    seen: list[str] = []

    run = await decide_from_stream(
        source,
        services=services,
        client=client,
        max_events=3,
        on_decision=lambda event, decision: seen.append(event.event_id),
    )

    expected = ["evt-stream-1", "evt-stream-2", "evt-stream-3"]
    assert [s.event_id for s in client.submitted] == expected
    assert seen == expected
    assert source.groups_ensured == 1
    assert (run.read, run.decided, run.dead_lettered, run.refused, run.failed) == (
        3,
        3,
        0,
        0,
        0,
    )
    assert run.actions == {"ALLOW": 3}
    assert source.dead_letters == []


async def test_a_run_never_reads_past_max_events(services, stream):
    source = FakeSource(stream(5))

    run = await decide_from_stream(
        source, services=services, client=ScriptedClient(), max_events=3, batch_size=2
    )

    assert source.new_reads == [2, 1]
    assert run.read == 3
    assert [message_id for message_id, _ in source.unread] == ["4-0", "5-0"]


async def test_a_message_the_consumer_dead_letters_is_not_decided(services, stream):
    messages = stream(2)
    source = FakeSource([messages[0], ("9-0", '{"not": "a login"}'), messages[1]])
    client = ScriptedClient()

    run = await decide_from_stream(
        source, services=services, client=client, max_events=3
    )

    assert [s.event_id for s in client.submitted] == ["evt-stream-1", "evt-stream-2"]
    assert (run.read, run.decided, run.dead_lettered) == (3, 2, 1)
    [entry] = source.dead_letters
    assert (entry["original_id"], entry["reason"]) == ("9-0", "schema_error")


@pytest.mark.parametrize(
    "refusal",
    [
        LedgerError(422, "NATIVE_SUBJECT_MISMATCH", "entity_id contradicts it"),
        LedgerError(422, "GATE_CONFIG_INVALID", "gate_config does not satisfy it"),
        LedgerError(422, "SUBMISSION_INVALID", "the body is not a valid Submission"),
        LedgerError(413, "BODY_TOO_LARGE", "the body is too large"),
    ],
    ids=lambda err: err.code,
)
async def test_a_refused_login_is_dead_lettered_and_the_run_goes_on(
    services, stream, refusal
):
    messages = stream(2)
    source = FakeSource(messages)
    client = ScriptedClient(errors={"evt-stream-1": refusal})

    run = await decide_from_stream(
        source, services=services, client=client, max_events=2
    )

    assert [s.event_id for s in client.submitted] == ["evt-stream-2"]
    assert (run.read, run.decided, run.refused, run.failed) == (2, 1, 1, 0)
    [entry] = source.dead_letters
    assert entry == {
        "original_id": "1-0",
        "data": messages[0][1],
        "reason": LEDGER_REFUSED,
        "detail": f"{refusal.status} {refusal.code}: {refusal.detail}",
        "event_id": "evt-stream-1",
    }


@pytest.mark.parametrize(
    "fatal",
    [
        LedgerError(401, "UNAUTHENTICATED", "no valid key"),
        LedgerError(403, "REASONER_MISMATCH", "the key is another reasoner's"),
        LedgerError(422, "REASONER_NOT_REGISTERED", "not registered here"),
        LedgerError(500, "INTERNAL_ERROR", "the ledger failed"),
        LedgerError(502, "HTTP_502", "bad gateway"),
    ],
    ids=lambda err: err.code,
)
async def test_a_ledger_error_that_would_refuse_every_login_stops_the_run(
    services, stream, fatal
):
    """A wrong key or a failing ledger must not drain the stream into the DLQ."""
    source = FakeSource(stream(3))
    client = ScriptedClient(errors={"evt-stream-1": fatal})
    run = StreamRun()

    with pytest.raises(LedgerError):
        await decide_from_stream(
            source, services=services, client=client, max_events=3, run=run
        )

    assert (run.read, run.refused, run.failed) == (1, 0, 1)
    [entry] = source.dead_letters
    assert entry["reason"] == PIPELINE_ERROR
    assert entry["detail"] == f"the decision failed ({fatal.status} {fatal.code})"
    assert [message_id for message_id, _ in source.pending] == ["2-0", "3-0"]


async def test_an_unreachable_ledger_dead_letters_the_login_and_stops_the_run(
    services, stream
):
    """The error's type is recorded, never its text; the rest stays pending."""
    unreachable = httpx.ConnectError("connect failed: key=sk-secret-in-message")
    source = FakeSource(stream(3))
    client = ScriptedClient(errors={"evt-stream-1": unreachable})
    run = StreamRun()

    with pytest.raises(httpx.ConnectError):
        await decide_from_stream(
            source, services=services, client=client, max_events=3, run=run
        )

    assert (run.read, run.decided, run.failed) == (1, 0, 1)
    assert source.checked == ["1-0"]
    [entry] = source.dead_letters
    assert entry["reason"] == PIPELINE_ERROR
    assert entry["event_id"] == "evt-stream-1"
    assert "ConnectError" in str(entry["detail"])
    assert "sk-secret" not in str(entry["detail"])
    assert client.submitted == []
    assert [message_id for message_id, _ in source.pending] == ["2-0", "3-0"]


async def test_the_next_run_decides_what_a_stopped_run_left_pending_first(
    services, stream
):
    source = FakeSource(stream(4))
    down = ScriptedClient(errors={"evt-stream-1": httpx.ConnectError("down")})
    with pytest.raises(httpx.ConnectError):
        await decide_from_stream(
            source, services=services, client=down, max_events=4, batch_size=3
        )
    assert [message_id for message_id, _ in source.pending] == ["2-0", "3-0"]
    client = ScriptedClient()

    run = await decide_from_stream(
        source, services=services, client=client, max_events=3, batch_size=3
    )

    assert [s.event_id for s in client.submitted] == [
        "evt-stream-2",
        "evt-stream-3",
        "evt-stream-4",
    ]
    assert source.reads[-3:] == [(3, True), (1, True), (1, False)]
    assert source.pending == []
    assert run.decided == 3


async def test_a_failing_dead_letter_write_does_not_hide_the_decisions_error(
    services, stream
):
    """When Redis is down the DLQ write fails too; the run raises the first error."""

    class _DeadLetterDown(FakeSource):
        def dead_letter(self, *args: object, **kwargs: object) -> None:
            raise ConnectionError("redis is down")

    source = _DeadLetterDown(stream(1))
    client = ScriptedClient(errors={"evt-stream-1": httpx.ConnectError("down")})

    with pytest.raises(httpx.ConnectError):
        await decide_from_stream(source, services=services, client=client, max_events=1)


async def test_stop_ends_the_run_after_the_messages_already_read(services, stream):
    source = FakeSource(stream(5))
    stop = asyncio.Event()

    run = await decide_from_stream(
        source,
        services=services,
        client=ScriptedClient(),
        batch_size=3,
        stop=stop,
        on_decision=lambda event, decision: stop.set(),
    )

    assert source.new_reads == [3]
    assert run.decided == 3
    assert source.pending == []
    assert [message_id for message_id, _ in source.unread] == ["4-0", "5-0"]


async def test_a_run_asked_to_stop_reads_nothing(services, stream):
    source = FakeSource(stream(2))
    stop = asyncio.Event()
    stop.set()

    run = await decide_from_stream(
        source, services=services, client=ScriptedClient(), stop=stop
    )

    assert source.reads == []
    assert run.read == 0


@pytest.mark.parametrize(
    ("max_events", "batch_size", "block_ms"),
    [(0, 10, 100), (-1, 10, 100), (None, 0, 100), (5, -2, 100), (5, 10, 0)],
)
async def test_the_limits_are_positive(services, max_events, batch_size, block_ms):
    with pytest.raises(ValueError, match="positive"):
        await decide_from_stream(
            FakeSource([]),
            services=services,
            client=ScriptedClient(),
            max_events=max_events,
            batch_size=batch_size,
            block_ms=block_ms,
        )


@pytest.fixture
def consume_command(monkeypatch, services):
    """``consume`` over the doubles: returns its source and client."""

    def _wire(
        messages: list[tuple[str, str]], errors: dict[str, Exception] | None = None
    ) -> tuple[FakeSource, ScriptedClient]:
        source = FakeSource(messages)
        client = ScriptedClient(errors)
        monkeypatch.setattr(cli, "build_ato_services", lambda settings: services)
        monkeypatch.setattr(cli, "ledger_client", lambda settings: client)
        monkeypatch.setattr(cli.redis.Redis, "from_url", MagicMock())
        monkeypatch.setattr(cli, "IngestionConsumer", lambda *a, **kw: source)
        return source, client

    return _wire


def test_consume_prints_each_decision_and_the_runs_counts(
    capsys, consume_command, stream
):
    source, client = consume_command(stream(3))

    assert cli.main(["consume", "--max-events", "2", "--block-ms", "10"]) == 0

    out, err = capsys.readouterr()
    lines = [json.loads(line) for line in out.splitlines()]
    assert [line["event_id"] for line in lines] == ["evt-stream-1", "evt-stream-2"]
    assert {line["decision_action"] for line in lines} == {"ALLOW"}
    assert {line["bypass_rule_id"] for line in lines} == {"allow-band"}
    summary = json.loads(err.splitlines()[-1])
    assert summary["read"] == 2
    assert summary["decided"] == 2
    assert summary["actions"] == {"ALLOW": 2}
    assert client.closed
    assert len(source.unread) == 1


def test_consume_exits_1_when_a_decision_fails(capsys, consume_command, stream):
    unreachable = httpx.ConnectError("connect failed")
    source, client = consume_command(stream(2), {"evt-stream-1": unreachable})

    assert cli.main(["consume", "--max-events", "2"]) == 1

    _, err = capsys.readouterr()
    assert "consume stopped: a decision failed (ConnectError)" in err
    assert json.loads(err.splitlines()[-1])["failed"] == 1
    assert source.dead_letters[0]["reason"] == PIPELINE_ERROR
    assert client.closed


def test_consume_exits_1_naming_a_stream_failure_no_decision_caused(
    capsys, consume_command, stream
):
    """Redis down: nothing can be dead-lettered, and no decision is blamed."""
    source, client = consume_command(stream(2))

    def _down() -> None:
        raise ConnectionError("Error 61 connecting to localhost:6379.")

    source.ensure_group = _down  # type: ignore[method-assign]

    assert cli.main(["consume", "--max-events", "2"]) == 1

    _, err = capsys.readouterr()
    assert "consume stopped: ConnectionError: Error 61 connecting" in err
    assert "a decision failed" not in err
    assert json.loads(err.splitlines()[-1])["read"] == 0
    assert source.dead_letters == []
    assert client.closed


@pytest.mark.parametrize("flag", ["--max-events", "--batch-size", "--block-ms"])
def test_consume_refuses_a_limit_below_one(capsys, flag):
    with pytest.raises(SystemExit) as caught:
        cli.main(["consume", flag, "0"])
    assert caught.value.code == 2
    assert "not a positive integer" in capsys.readouterr().err
