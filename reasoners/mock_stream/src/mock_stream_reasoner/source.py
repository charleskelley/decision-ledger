"""The stream mock's event source — an async, in-memory stream.

``EventSource`` stands in for a message broker. Producers ``publish`` raw
events (JSON-shaped, as a broker would deliver them); the consumer iterates
the accepted ones in arrival order (``events``), or takes what is queued
without waiting (``take_ready``). On the way in, the source:

- validates each payload as a ``TransferEvent``; one that is not is
  dead-lettered as ``malformed``, with the validation errors;
- drops a second delivery of an ``event_id`` it has seen (a broker delivers
  at least once; the source makes it effectively once), and counts it;
- keeps a watermark, the latest event time it accepted, and dead-letters an
  event older than the watermark by more than ``allowed_lateness`` as
  ``late``: the per-account windows could no longer place it.

The consumer dead-letters what the ledger refuses (``rejected``, with the
problem's code), cannot be reached for (``unreachable``), or what fails on
the way (``failed``) through ``dead_letter``, so nothing the stream carried
is lost silently. ``reset``
forgets the dedup set and the watermark (the eval harness resets between
orderings); dead letters are kept for inspection.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal, get_args

from pydantic import ValidationError

from mock_stream_reasoner.events import TransferEvent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

#: How far behind the watermark an event may arrive and still be accepted.
ALLOWED_LATENESS = timedelta(minutes=5)
#: How many event ids the dedup set remembers (oldest forgotten first).
DEDUP_CAPACITY = 100_000

#: Why an event left the stream without a decision.
DeadLetterReason = Literal["malformed", "late", "rejected", "unreachable", "failed"]

#: What ``publish`` did with one payload.
PublishOutcome = Literal["accepted", "duplicate", "dead_lettered"]


@dataclass(frozen=True)
class DeadLetter:
    """An event the stream carried but could not decide.

    Attributes:
        reason: ``malformed``, ``late``, ``rejected``, ``unreachable``, or
            ``failed``.
        event_id: The event's id, when it had a readable one.
        detail: Why, in words (validation errors, the lateness, the
            ledger's problem code and detail, the transport error).
        payload: The event as received (JSON-shaped), or its ``repr`` when it
            was not JSON at all.
    """

    reason: DeadLetterReason
    event_id: str | None
    detail: str
    payload: object


@dataclass(frozen=True)
class Published:
    """What ``publish`` did with one payload.

    Attributes:
        outcome: ``accepted`` (queued for the consumer), ``duplicate``
            (dropped: its ``event_id`` was seen), or ``dead_lettered``.
        event_id: The payload's event id, when it had a readable one.
        reason: The dead-letter reason, for a dead-lettered payload.
    """

    outcome: PublishOutcome
    event_id: str | None
    reason: DeadLetterReason | None = None


class _Closed:
    """The end-of-stream marker ``close`` queues."""


_CLOSED = _Closed()


class EventSource:
    """An async in-memory stream with dedup, a lateness bound, and dead letters.

    The queue is unbounded: ``publish`` never waits for the consumer, and
    ``pending`` says how far behind it is.

    Args:
        allowed_lateness: How far behind the watermark an event may arrive.
        dedup_capacity: Event ids the dedup set remembers.
    """

    def __init__(
        self,
        *,
        allowed_lateness: timedelta = ALLOWED_LATENESS,
        dedup_capacity: int = DEDUP_CAPACITY,
    ) -> None:
        """Open an empty stream."""
        if allowed_lateness < timedelta(0):
            msg = f"allowed_lateness must not be negative, got {allowed_lateness}"
            raise ValueError(msg)
        self.allowed_lateness = allowed_lateness
        self._capacity = max(dedup_capacity, 1)
        self._queue: asyncio.Queue[TransferEvent | _Closed] = asyncio.Queue()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._watermark: datetime | None = None
        self._dead: list[DeadLetter] = []
        self._counts: Counter[str] = Counter()
        self._closed = False
        self._marker_queued = False

    @property
    def watermark(self) -> datetime | None:
        """The latest event time accepted since the last ``reset``."""
        return self._watermark

    @property
    def pending(self) -> int:
        """Accepted events waiting for the consumer."""
        return self._queue.qsize() - (1 if self._marker_queued else 0)

    @property
    def dead_letters(self) -> tuple[DeadLetter, ...]:
        """Every dead letter, oldest first."""
        return tuple(self._dead)

    def stats(self) -> dict[str, int]:
        """Counters since the source opened (``reset`` keeps them).

        Returns:
            ``received``, ``accepted``, ``duplicates``, and ``dead_lettered``
            in total and per reason (``dead_lettered_<reason>``).
        """
        counts = {
            "received": self._counts["received"],
            "accepted": self._counts["accepted"],
            "duplicates": self._counts["duplicates"],
            "dead_lettered": len(self._dead),
        }
        for reason in get_args(DeadLetterReason):
            counts[f"dead_lettered_{reason}"] = self._counts[f"dead:{reason}"]
        return counts

    async def publish(self, payload: object) -> Published:
        """Offer one raw event to the stream.

        Args:
            payload: The event as a producer sent it (JSON-shaped).

        Returns:
            What the source did with it.

        Raises:
            RuntimeError: The stream is closed.
        """
        if self._closed:
            msg = "the event source is closed"
            raise RuntimeError(msg)
        self._counts["received"] += 1
        event_id = _readable_event_id(payload)
        try:
            event = TransferEvent.from_payload(payload)
        except ValidationError as err:
            detail = "; ".join(
                f"{'.'.join(str(part) for part in e['loc']) or '<root>'}: {e['msg']}"
                for e in err.errors(include_url=False)
            )
            self.dead_letter(
                "malformed", event_id=event_id, detail=detail, payload=payload
            )
            return Published("dead_lettered", event_id, "malformed")
        except (TypeError, ValueError) as err:
            self.dead_letter(
                "malformed", event_id=event_id, detail=str(err), payload=payload
            )
            return Published("dead_lettered", event_id, "malformed")
        if event.event_id in self._seen:
            self._seen.move_to_end(event.event_id)
            self._counts["duplicates"] += 1
            return Published("duplicate", event.event_id)
        if (
            self._watermark is not None
            and event.timestamp < self._watermark - self.allowed_lateness
        ):
            behind = self._watermark - event.timestamp
            self.dead_letter(
                "late",
                event_id=event.event_id,
                detail=(
                    f"event time {event.timestamp.isoformat()} is {behind} behind the "
                    f"watermark {self._watermark.isoformat()} (allowed lateness "
                    f"{self.allowed_lateness})"
                ),
                payload=payload,
            )
            return Published("dead_lettered", event.event_id, "late")
        self._seen[event.event_id] = None
        while len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        if self._watermark is None or event.timestamp > self._watermark:
            self._watermark = event.timestamp
        self._counts["accepted"] += 1
        self._queue.put_nowait(event)
        return Published("accepted", event.event_id)

    def dead_letter(
        self,
        reason: DeadLetterReason,
        *,
        event_id: str | None,
        detail: str,
        payload: object,
    ) -> DeadLetter:
        """Record an event the stream could not decide.

        Args:
            reason: Why.
            event_id: The event's id, if readable.
            detail: Why, in words.
            payload: The event as received.

        Returns:
            The dead letter.
        """
        letter = DeadLetter(
            reason=reason, event_id=event_id, detail=detail, payload=_jsonable(payload)
        )
        self._dead.append(letter)
        self._counts[f"dead:{reason}"] += 1
        return letter

    def take_ready(self) -> list[TransferEvent]:
        """Take every accepted event queued now, without waiting.

        Returns:
            The events in arrival order (the end-of-stream marker, if
            queued, is put back).
        """
        events: list[TransferEvent] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return events
            self._queue.task_done()
            if isinstance(item, _Closed):
                self._queue.put_nowait(item)
                return events
            events.append(item)

    async def events(self) -> AsyncIterator[TransferEvent]:
        """Yield accepted events in arrival order until the stream closes.

        Yields:
            Each accepted event; after ``close``, the ones already queued,
            then iteration ends.
        """
        while True:
            item = await self._queue.get()
            try:
                if isinstance(item, _Closed):
                    self._marker_queued = False
                    return
                yield item
            finally:
                self._queue.task_done()

    async def drained(self) -> None:
        """Wait until the consumer has finished every accepted event."""
        await self._queue.join()

    def close(self) -> None:
        """End the stream: ``events`` stops once the queued events are taken."""
        if not self._closed:
            self._closed = True
            self._marker_queued = True
            self._queue.put_nowait(_CLOSED)

    def reset(self) -> None:
        """Forget the dedup set and the watermark (dead letters are kept)."""
        self._seen.clear()
        self._watermark = None


def _readable_event_id(payload: object) -> str | None:
    """The payload's ``event_id``, when it carries a non-empty string one."""
    if isinstance(payload, dict):
        event_id = payload.get("event_id")
        if isinstance(event_id, str) and event_id:
            return event_id
    return None


def _jsonable(payload: object) -> object:
    """``payload`` when it is JSON, else its ``repr``."""
    try:
        json.dumps(payload)
    except (TypeError, ValueError):
        return repr(payload)
    return payload
