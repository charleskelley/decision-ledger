"""The stream mock's HTTP surface: produce into the stream, watch it decide.

``router`` serves the routes under ``/api/v1/stream``. A producer posts raw
events (``POST /events``, one JSON object or an array, as a broker would hand
them over); the source validates, deduplicates, and checks lateness at once,
answers what it did with each, and the consumer task decides the accepted
ones in arrival order. The rest let an operator watch the stream:
``/stats`` (counters, the watermark, and whether the consumer has caught
up), ``/decisions`` (the most recent decisions), ``/dead-letters``, and
``/reset`` (forget the per-account state, the dedup set, and the watermark).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from mock_stream_reasoner.registry import MOCK_STREAM_REGISTRATION

if TYPE_CHECKING:
    import asyncio

    from app.client import LedgerClient
    from mock_stream_reasoner.pipeline import StreamProcessor
    from mock_stream_reasoner.settings import MockStreamSettings
    from mock_stream_reasoner.source import EventSource

#: Events one ``POST /events`` may carry.
MAX_EVENTS_PER_REQUEST = 10_000


@dataclass
class StreamRuntime:
    """What the service runs: the stream, its consumer, and the ledger client.

    Attributes:
        settings: The service's settings.
        client: The ledger the consumer submits to.
        source: The stream.
        processor: The consumer's reasoner loop.
        consumer: The task that runs ``processor.run(source)``.
    """

    settings: MockStreamSettings
    client: LedgerClient
    source: EventSource
    processor: StreamProcessor
    consumer: asyncio.Task[None]


class PublishResult(BaseModel):
    """What the source did with one posted event."""

    event_id: str | None
    outcome: str
    reason: str | None


class PublishResponse(BaseModel):
    """What the source did with a posted batch."""

    published: int
    accepted: int
    duplicates: int
    dead_lettered: int
    results: list[PublishResult]


class StreamStats(BaseModel):
    """The stream's counters and progress."""

    reasoner_id: str
    source: dict[str, int]
    processor: dict[str, int]
    pending: int
    accounts: int
    history: int
    watermark: str | None
    caught_up: bool


router = APIRouter(prefix="/api/v1/stream", tags=["stream"])


def _runtime(request: Request) -> StreamRuntime:
    return request.app.state.stream


@router.post("/events", response_model=PublishResponse)
async def publish_events(request: Request) -> PublishResponse:
    """Offer raw events to the stream.

    The body is one event object or an array of them. Each is validated,
    deduplicated, and checked for lateness now; a malformed or late one is
    dead-lettered, not refused, as a broker would carry it anyway.

    Returns:
        What the source did with each event, in order.

    Raises:
        HTTPException: 422 when the body is not JSON, not an object or an
            array, or carries more than ``MAX_EVENTS_PER_REQUEST`` events.
    """
    try:
        body: Any = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise HTTPException(422, detail=f"the body is not JSON: {err}") from err
    if not isinstance(body, list | dict):
        raise HTTPException(422, detail="the body must be an event or an array")
    events = body if isinstance(body, list) else [body]
    if len(events) > MAX_EVENTS_PER_REQUEST:
        raise HTTPException(
            422, detail=f"at most {MAX_EVENTS_PER_REQUEST} events per request"
        )
    runtime = _runtime(request)
    results = [await runtime.source.publish(event) for event in events]
    return PublishResponse(
        published=len(results),
        accepted=sum(r.outcome == "accepted" for r in results),
        duplicates=sum(r.outcome == "duplicate" for r in results),
        dead_lettered=sum(r.outcome == "dead_lettered" for r in results),
        results=[
            PublishResult(event_id=r.event_id, outcome=r.outcome, reason=r.reason)
            for r in results
        ],
    )


@router.get("/stats", response_model=StreamStats)
async def stream_stats(request: Request) -> StreamStats:
    """The stream's counters, its watermark, and whether it has caught up."""
    runtime = _runtime(request)
    source = runtime.source.stats()
    processor = runtime.processor.stats()
    processed = processor.get("decided", 0) + processor.get("dead_lettered", 0)
    watermark = runtime.source.watermark
    return StreamStats(
        reasoner_id=MOCK_STREAM_REGISTRATION.reasoner_id,
        source=source,
        processor=processor,
        pending=runtime.source.pending,
        accounts=len(runtime.processor.windows),
        history=runtime.processor.history,
        watermark=watermark.isoformat() if watermark is not None else None,
        caught_up=processed >= source["accepted"],
    )


@router.get("/decisions")
async def stream_decisions(
    request: Request, limit: int = Query(default=100, ge=1, le=100_000)
) -> list[dict[str, Any]]:
    """The most recent decisions, oldest first (at most ``limit``).

    The service keeps its last ``history`` decisions (``MOCK_STREAM_HISTORY``,
    in ``/stats``); the ledger holds every one.
    """
    decisions = _runtime(request).processor.decisions
    return [asdict(decision) for decision in decisions[-limit:]]


@router.get("/dead-letters")
async def stream_dead_letters(request: Request) -> list[dict[str, Any]]:
    """Every dead letter, oldest first."""
    return [asdict(letter) for letter in _runtime(request).source.dead_letters]


@router.post("/reset", response_model=StreamStats)
async def reset_stream(request: Request) -> StreamStats:
    """Forget the per-account state, the dedup set, and the watermark."""
    runtime = _runtime(request)
    runtime.processor.windows.reset()
    runtime.source.reset()
    return await stream_stats(request)
