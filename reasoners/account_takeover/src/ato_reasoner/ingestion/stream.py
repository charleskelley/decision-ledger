"""The async adapter between ATO's stream consumer and its pipeline.

``IngestionConsumer`` is synchronous: it reads ``ato:login-events`` through a
Redis consumer group and validates, dead-letters, deduplicates, and ACKs each
message. ``run_ato_decision`` is a coroutine: it scores a login and awaits
the ledger. ``decide_from_stream`` joins them. It reads a batch in a worker
thread (an ``XREADGROUP`` blocks for up to ``block_ms``), passes each message
through the consumer's checks, and decides every accepted event in stream
order, one at a time, on the event loop: the pipeline ATO's service route
runs, so a login decided from the stream and one posted to
``POST /api/v1/ato/decisions`` make the same submission.

Delivery. A run first re-reads this consumer's backlog (messages an earlier
run read but never checked, because it stopped), then reads new messages.
The consumer ACKs a message once its checks pass, before the decision (the
dedup window already makes delivery at most once per event id), so a
decision that fails is dead-lettered, not redelivered:

- the ledger refuses the submission itself (413, or 422 other than
  ``REASONER_NOT_REGISTERED``): reason ``ledger_refused``, with the
  problem's status, code, and detail; the run goes on to the next message;
- any other failure of the decision: a ledger error that would refuse every
  login alike (401 or 403 for the key, ``REASONER_NOT_REGISTERED``, a 5xx),
  the ledger unreachable, the scorer or the feature windows failing: reason
  ``pipeline_error``, with the error's type (and a ledger error's status and
  code) but never its text (an ``httpx`` message can quote the ledger's key),
  and the error stops the run. The rest of that batch stays pending, and the
  next run decides it first.

When Redis itself fails, nothing can be dead-lettered (the dead-letter stream
is in that Redis): the error stops the run, and a message checked but not yet
decided is lost with it. A hard stop (a second SIGINT) loses the login being
decided the same way: it was ACKed, so it is neither decided nor
dead-lettered, and its payload stays in the stream.

A run ends after ``max_events`` stream messages (decided or dead-lettered;
it never reads past that many), or, without a limit, once ``stop`` is set
(``python -m ato_reasoner consume`` sets it on SIGINT or SIGTERM). Either way
it finishes the messages it has already read before it returns.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import structlog

from ato_reasoner.pipeline import run_ato_decision
from core.exceptions import LedgerError

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.client import LedgerClient
    from ato_reasoner.events import LoginEvent
    from ato_reasoner.pipeline import AtoDecision
    from ato_reasoner.plugin import AtoServices

log = structlog.get_logger(__name__)

#: Dead-letter reason of an accepted event whose submission the ledger refused.
LEDGER_REFUSED = "ledger_refused"
#: Dead-letter reason of an accepted event whose decision failed otherwise.
PIPELINE_ERROR = "pipeline_error"

#: The one 422 the ledger answers for every submission of an unregistered
#: reasoner: a deployment problem, not a problem with the login.
_UNREGISTERED = "REASONER_NOT_REGISTERED"


def refuses_the_login(err: LedgerError) -> bool:
    """Whether a ledger error refuses this submission rather than every one.

    The ledger refuses a submission's content with 422 (a contract code) or
    413 (too large); those dead-letter the one login and the run goes on.
    Every other ledger error (an unauthenticated or mismatched key, an
    unregistered reasoner, a ledger that failed) would refuse the next login
    alike, so it stops the run instead of draining the stream into the
    dead-letter stream.

    Args:
        err: The ledger's error.

    Returns:
        True for a refusal of this submission alone.
    """
    return err.status in (413, 422) and err.code != _UNREGISTERED


class EventSource(Protocol):
    """What ``decide_from_stream`` needs from a stream consumer.

    ``IngestionConsumer`` is the implementation; see its methods.
    """

    def ensure_group(self) -> None:
        """Create the consumer group if it does not exist."""
        ...

    def read(
        self, *, batch_size: int, block_ms: int, pending: bool = False
    ) -> list[tuple[str, str]]:
        """Read new messages, or (``pending``) this consumer's backlog."""
        ...

    def process_message(self, message_id: str, raw_data: str) -> LoginEvent | None:
        """Check and ACK one message; the event when it is accepted."""
        ...

    def dead_letter(
        self,
        message_id: str,
        raw_data: str,
        *,
        reason: str,
        detail: str,
        event_id: str | None = None,
    ) -> None:
        """Copy a message to the dead-letter stream."""
        ...


@dataclass
class StreamRun:
    """What one run of ``decide_from_stream`` did, counted as it goes.

    A message counts as read once the run starts checking it, and then
    lands in exactly one of the other counts, unless Redis fails or the run
    is interrupted while it is in hand.

    Args:
        read: Messages the run took from the stream and checked.
        decided: Accepted events the ledger decided.
        dead_lettered: Messages the consumer's checks dead-lettered (schema
            error, lateness, duplicate).
        refused: Accepted events whose submission the ledger refused
            (dead-lettered as ``ledger_refused``).
        failed: Accepted events whose decision failed otherwise
            (dead-lettered as ``pipeline_error``; the run stopped).
        actions: Decided events by final action.
    """

    read: int = 0
    decided: int = 0
    dead_lettered: int = 0
    refused: int = 0
    failed: int = 0
    actions: Counter[str] = field(default_factory=Counter)


async def decide_from_stream(
    source: EventSource,
    *,
    services: AtoServices,
    client: LedgerClient,
    max_events: int | None = None,
    batch_size: int = 10,
    block_ms: int = 1_000,
    stop: asyncio.Event | None = None,
    run: StreamRun | None = None,
    on_decision: Callable[[LoginEvent, AtoDecision], None] | None = None,
) -> StreamRun:
    """Decide the stream's accepted logins through ATO's pipeline.

    Args:
        source: The stream consumer (``IngestionConsumer``).
        services: ATO's services (feature windows, scorer).
        client: The ledger, in process or over HTTP.
        max_events: Stop after this many stream messages; ``None`` runs
            until ``stop`` is set.
        batch_size: Maximum messages per read.
        block_ms: Milliseconds a read blocks waiting for new messages (how
            long a set ``stop`` can go unnoticed on an idle stream).
        stop: Set it to end the run after the messages already read.
        run: Counts to accumulate into, so a caller keeps them when the run
            raises; a fresh ``StreamRun`` when omitted.
        on_decision: Called with each decided event and its decision.

    Returns:
        The run's counts.

    Raises:
        ValueError: ``max_events``, ``batch_size``, or ``block_ms`` is below
            1.
        Exception: Whatever failed a decision other than a refusal of its
            login (after the message was dead-lettered as
            ``pipeline_error``), or whatever the stream itself raised.
    """
    if max_events is not None and max_events < 1:
        msg = "max_events is a positive number of messages (or None for no limit)"
        raise ValueError(msg)
    if batch_size < 1:
        msg = "batch_size is a positive number of messages"
        raise ValueError(msg)
    if block_ms < 1:
        msg = "block_ms is a positive number of milliseconds (0 blocks forever)"
        raise ValueError(msg)
    run = run if run is not None else StreamRun()
    await asyncio.to_thread(source.ensure_group)
    backlog = True
    while max_events is None or run.read < max_events:
        if stop is not None and stop.is_set():
            break
        count = (
            batch_size if max_events is None else min(batch_size, max_events - run.read)
        )
        messages = await asyncio.to_thread(
            source.read, batch_size=count, block_ms=block_ms, pending=backlog
        )
        if backlog and not messages:
            backlog = False
            continue
        for position, (message_id, raw) in enumerate(messages):
            run.read += 1
            event = await asyncio.to_thread(source.process_message, message_id, raw)
            if event is None:
                run.dead_lettered += 1
                continue
            try:
                decision = await run_ato_decision(
                    event=event, services=services, client=client
                )
            except Exception as err:
                if isinstance(err, LedgerError) and refuses_the_login(err):
                    await asyncio.to_thread(
                        source.dead_letter,
                        message_id,
                        raw,
                        reason=LEDGER_REFUSED,
                        detail=f"{err.status} {err.code}: {err.detail}",
                        event_id=event.event_id,
                    )
                    run.refused += 1
                    log.warning(
                        "ato.stream.refused",
                        component="ato_stream",
                        event_id=event.event_id,
                        status=err.status,
                        code=err.code,
                    )
                    continue
                run.failed += 1
                await _dead_letter_failure(source, message_id, raw, event, err)
                log.error(
                    "ato.stream.failed",
                    component="ato_stream",
                    event_id=event.event_id,
                    error_type=type(err).__name__,
                    left_pending=len(messages) - position - 1,
                )
                raise
            run.decided += 1
            run.actions[decision.receipt.decision_action.value] += 1
            if on_decision is not None:
                on_decision(event, decision)
    log.info(
        "ato.stream.run_ended",
        component="ato_stream",
        read=run.read,
        decided=run.decided,
        dead_lettered=run.dead_lettered,
        refused=run.refused,
    )
    return run


async def _dead_letter_failure(
    source: EventSource,
    message_id: str,
    raw: str,
    event: LoginEvent,
    err: Exception,
) -> None:
    """Dead-letter a failed decision as ``pipeline_error``, never masking ``err``.

    The detail names the error's type, and a ledger error's status and code,
    never its text. When the dead-letter write fails too (Redis is down),
    that is logged and the decision's own error is the one the run raises.
    """
    detail = f"the decision failed ({type(err).__name__})"
    if isinstance(err, LedgerError):
        detail = f"the decision failed ({err.status} {err.code})"
    try:
        await asyncio.to_thread(
            source.dead_letter,
            message_id,
            raw,
            reason=PIPELINE_ERROR,
            detail=detail,
            event_id=event.event_id,
        )
    except Exception as dead_letter_err:
        log.error(
            "ato.stream.dead_letter_failed",
            component="ato_stream",
            event_id=event.event_id,
            error_type=type(dead_letter_err).__name__,
        )
