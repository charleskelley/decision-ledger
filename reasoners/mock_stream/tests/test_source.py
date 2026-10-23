"""The stream's source: arrival order, once-only delivery, lateness, dead letters.

``EventSource`` queues what it accepts in arrival order, drops a second
delivery of an event id (before it checks lateness), dead-letters an event
more than ``allowed_lateness`` behind its watermark as ``late`` and a payload
that is not a transfer as ``malformed``, and records what the consumer could
not decide (``dead_letter``). ``stats`` counts every reason; ``reset``
forgets the dedup set and the watermark but no dead letter or counter.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from mock_stream_reasoner.source import EventSource

#: Every dead-letter reason, as ``stats`` counts them.
REASONS = ("malformed", "late", "rejected", "unreachable", "failed")


async def _consume(source):
    """The ids the consumer iterates until the stream ends (bounded wait)."""

    async def drain():
        return [event.event_id async for event in source.events()]

    return await asyncio.wait_for(drain(), timeout=5)


def _counts(source):
    stats = source.stats()
    return {reason: stats[f"dead_lettered_{reason}"] for reason in REASONS}


async def test_accepted_events_are_taken_in_arrival_order_not_event_time(transfer):
    source = EventSource()
    events = [
        transfer(1, "acct-a", 10.0, minutes=3),
        transfer(2, "acct-b", 20.0, minutes=0),
        transfer(3, "acct-a", 30.0, minutes=2),
    ]

    published = [await source.publish(event.payload()) for event in events]

    assert [(p.outcome, p.event_id, p.reason) for p in published] == [
        ("accepted", "evt-test-0001", None),
        ("accepted", "evt-test-0002", None),
        ("accepted", "evt-test-0003", None),
    ]
    assert source.pending == 3
    assert source.take_ready() == events
    assert source.pending == 0
    assert source.take_ready() == []


async def test_the_consumer_iterates_in_arrival_order_until_the_stream_closes(
    transfer,
):
    source = EventSource()
    consumer = asyncio.create_task(_consume(source))

    for n in (1, 2, 3):
        await source.publish(transfer(n, "acct-a", 10.0 * n, minutes=4 - n).payload())
    source.close()

    assert await consumer == ["evt-test-0001", "evt-test-0002", "evt-test-0003"]
    await asyncio.wait_for(source.drained(), timeout=5)


async def test_closing_ends_the_stream_once_the_queued_events_are_taken(transfer):
    source = EventSource()
    for n in (1, 2):
        await source.publish(transfer(n, "acct-a", 10.0).payload())

    source.close()

    assert source.pending == 2  # the end-of-stream marker is not an event
    assert await _consume(source) == ["evt-test-0001", "evt-test-0002"]
    assert source.pending == 0


async def test_taking_the_ready_events_of_a_closed_stream_keeps_its_end(transfer):
    source = EventSource()
    await source.publish(transfer(1, "acct-a", 10.0).payload())
    source.close()

    assert [event.event_id for event in source.take_ready()] == ["evt-test-0001"]
    assert source.pending == 0
    assert await _consume(source) == []


async def test_publishing_to_a_closed_stream_is_refused(transfer):
    source = EventSource()
    source.close()

    with pytest.raises(RuntimeError, match="closed"):
        await source.publish(transfer(1, "acct-a", 10.0).payload())


async def test_a_second_delivery_of_an_event_id_is_dropped_and_counted(transfer):
    source = EventSource()
    first = transfer(1, "acct-a", 10.0)
    same_id_other_body = transfer(1, "acct-a", 999.0, minutes=1)

    published = [
        await source.publish(first.payload()),
        await source.publish(first.payload()),
        await source.publish(same_id_other_body.payload()),
    ]

    assert [(p.outcome, p.event_id) for p in published] == [
        ("accepted", "evt-test-0001"),
        ("duplicate", "evt-test-0001"),
        ("duplicate", "evt-test-0001"),
    ]
    assert source.take_ready() == [first]
    stats = source.stats()
    assert (stats["received"], stats["accepted"], stats["duplicates"]) == (3, 1, 2)
    assert stats["dead_lettered"] == 0
    assert source.dead_letters == ()


async def test_a_redelivery_is_a_duplicate_even_when_it_would_also_be_late(
    transfer,
):
    source = EventSource()
    early = transfer(1, "acct-a", 10.0, minutes=0)
    await source.publish(early.payload())
    await source.publish(transfer(2, "acct-a", 10.0, minutes=30).payload())

    redelivered = await source.publish(early.payload())

    assert (redelivered.outcome, redelivered.reason) == ("duplicate", None)
    assert source.dead_letters == ()
    assert source.stats()["duplicates"] == 1
    assert _counts(source)["late"] == 0


async def test_an_event_further_behind_the_watermark_than_allowed_is_dead_lettered_late(
    transfer, t0
):
    source = EventSource(allowed_lateness=timedelta(minutes=5))
    await source.publish(transfer(1, "acct-a", 10.0, minutes=20).payload())
    late = transfer(2, "acct-b", 10.0, minutes=14)

    published = await source.publish(late.payload())

    assert (published.outcome, published.event_id, published.reason) == (
        "dead_lettered",
        "evt-test-0002",
        "late",
    )
    [letter] = source.dead_letters
    assert (letter.reason, letter.event_id, letter.payload) == (
        "late",
        "evt-test-0002",
        late.payload(),
    )
    assert source.watermark == t0 + timedelta(minutes=20)
    assert source.watermark.isoformat() in letter.detail
    assert [event.event_id for event in source.take_ready()] == ["evt-test-0001"]
    assert _counts(source) == {
        "malformed": 0,
        "late": 1,
        "rejected": 0,
        "unreachable": 0,
        "failed": 0,
    }


@pytest.mark.parametrize("minutes_behind", [1.0, 4.5, 5.0])
async def test_an_event_within_the_allowed_lateness_is_accepted_and_keeps_the_watermark(
    transfer, t0, minutes_behind
):
    source = EventSource(allowed_lateness=timedelta(minutes=5))
    assert source.watermark is None
    await source.publish(transfer(1, "acct-a", 10.0, minutes=20).payload())

    published = await source.publish(
        transfer(2, "acct-a", 10.0, minutes=20 - minutes_behind).payload()
    )

    assert published.outcome == "accepted"
    assert source.watermark == t0 + timedelta(minutes=20)
    assert [event.event_id for event in source.take_ready()] == [
        "evt-test-0001",
        "evt-test-0002",
    ]
    assert source.dead_letters == ()


_VALID = {
    "event_id": "evt-x",
    "timestamp": "2026-10-01T09:00:00Z",
    "account_id": "acct-a",
    "amount": 10.0,
}


@pytest.mark.parametrize(
    ("payload", "event_id", "mentions", "is_json"),
    [
        pytest.param(
            {k: v for k, v in _VALID.items() if k != "amount"},
            "evt-x",
            "amount",
            True,
            id="missing-field",
        ),
        pytest.param({**_VALID, "amount": "ten"}, "evt-x", "amount", True, id="text"),
        pytest.param(
            {**_VALID, "timestamp": "yesterday"},
            "evt-x",
            "timestamp",
            True,
            id="not-a-time",
        ),
        pytest.param(
            {**_VALID, "event_id": 7}, None, "event_id", True, id="unreadable-id"
        ),
        pytest.param(
            {**_VALID, "amount": {10.0}},
            "evt-x",
            "not JSON serializable",
            False,
            id="not-json",
        ),
        pytest.param(["evt-x", "acct-a", 10.0], None, "<root>", True, id="a-list"),
        pytest.param("evt-x", None, "<root>", True, id="a-string"),
    ],
)
async def test_a_malformed_payload_is_dead_lettered_with_the_problem_in_words(
    payload, event_id, mentions, is_json
):
    source = EventSource()

    published = await source.publish(payload)

    assert (published.outcome, published.event_id, published.reason) == (
        "dead_lettered",
        event_id,
        "malformed",
    )
    [letter] = source.dead_letters
    assert (letter.reason, letter.event_id) == ("malformed", event_id)
    assert mentions in letter.detail
    assert letter.payload == (payload if is_json else repr(payload))
    assert source.pending == 0
    assert _counts(source)["malformed"] == 1
    assert source.stats()["accepted"] == 0


async def test_what_the_consumer_cannot_decide_is_dead_lettered_and_each_reason_counted(
    transfer,
):
    source = EventSource()
    for n in (1, 2, 3):
        await source.publish(transfer(n, "acct-a", 10.0, minutes=n).payload())
    await source.publish({"event_id": "evt-bad"})
    rejected, unreachable, failed = source.take_ready()

    letter = source.dead_letter(
        "rejected",
        event_id=rejected.event_id,
        detail="422 NATIVE_SUBJECT_MISMATCH: the subject does not match",
        payload=rejected.payload(),
    )
    source.dead_letter(
        "unreachable",
        event_id=unreachable.event_id,
        detail="ConnectError: connection refused",
        payload=unreachable.payload(),
    )
    source.dead_letter(
        "failed",
        event_id=failed.event_id,
        detail="RuntimeError: scorer crashed",
        payload={"not": {"json"}},
    )

    assert source.dead_letters[1] == letter
    assert [(d.reason, d.event_id) for d in source.dead_letters] == [
        ("malformed", "evt-bad"),
        ("rejected", "evt-test-0001"),
        ("unreachable", "evt-test-0002"),
        ("failed", "evt-test-0003"),
    ]
    assert letter.payload == rejected.payload()
    assert source.dead_letters[-1].payload == repr({"not": {"json"}})
    stats = source.stats()
    assert (stats["received"], stats["accepted"], stats["dead_lettered"]) == (4, 3, 4)
    assert _counts(source) == {
        "malformed": 1,
        "late": 0,
        "rejected": 1,
        "unreachable": 1,
        "failed": 1,
    }


async def test_reset_forgets_the_dedup_set_and_the_watermark_but_keeps_the_record(
    transfer,
):
    source = EventSource()
    seen = transfer(1, "acct-a", 10.0, minutes=20)
    early = transfer(2, "acct-a", 10.0, minutes=0)
    await source.publish(seen.payload())
    await source.publish(early.payload())  # 20 minutes behind: late
    letters, stats = source.dead_letters, source.stats()

    source.reset()

    assert source.watermark is None
    assert source.dead_letters == letters
    assert source.stats() == stats
    replayed = [
        await source.publish(early.payload()),
        await source.publish(seen.payload()),
    ]
    assert [p.outcome for p in replayed] == ["accepted", "accepted"]
    assert source.stats()["received"] == stats["received"] + 2
    assert source.stats()["accepted"] == stats["accepted"] + 2
    assert _counts(source)["late"] == 1


async def test_the_dedup_set_forgets_the_oldest_ids_beyond_its_capacity(transfer):
    source = EventSource(dedup_capacity=2)
    first, second, third = (transfer(n, "acct-a", 10.0) for n in (1, 2, 3))
    for event in (first, second, third):
        await source.publish(event.payload())

    assert (await source.publish(third.payload())).outcome == "duplicate"
    assert (await source.publish(first.payload())).outcome == "accepted"


def test_a_negative_allowed_lateness_is_refused():
    with pytest.raises(ValueError, match="allowed_lateness"):
        EventSource(allowed_lateness=timedelta(seconds=-1))
