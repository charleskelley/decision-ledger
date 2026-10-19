"""Redis Streams consumer for ATO Reasoner login events.

IngestionConsumer reads from a Redis Streams consumer group, validates
each message against LoginEvent, enforces a bounded-lateness window,
and deduplicates via IdempotencyStore. Messages that fail any check are
routed to the dead-letter stream; all messages are ACKed to prevent
infinite redelivery. ``consume`` hands each accepted event to a
synchronous callback; ``ato_reasoner.ingestion.stream.decide_from_stream``
drives the same checks (``read``, then ``process_message``) and decides each
accepted event through ATO's pipeline (``python -m ato_reasoner consume``).

Stream layout::

    Source:  ato:login-events          (produced by generator)
    DLQ:     ato:login-events:dlq      (validation / lateness / duplicate failures)

Each stream entry has a single ``"data"`` field containing a JSON-serialised
LoginEvent produced by ``model_dump_json()``.

DLQ entries copy the original ``"data"`` payload and add:

- ``reason``:      ``schema_error`` | ``lateness`` | ``duplicate`` (this
  consumer's checks), or ``ledger_refused`` | ``pipeline_error`` (an
  accepted event whose decision failed; written by ``decide_from_stream``)
- ``detail``:      human-readable description of the failure
- ``original_id``: Redis Streams message ID of the rejected entry
- ``event_id``:    parsed event_id when available (every reason but
  ``schema_error``)
"""

from __future__ import annotations

from collections.abc import Callable  # noqa: TC003
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from pydantic import ValidationError
from redis.exceptions import ResponseError

from ato_reasoner.events import LoginEvent
from ato_reasoner.ingestion.dedup import IdempotencyStore

if TYPE_CHECKING:
    import redis as redis_lib

log = structlog.get_logger(__name__)

#: The login-event stream the scenario generator publishes to.
DEFAULT_STREAM_KEY = "ato:login-events"
#: The consumer group ATO reads the stream through.
DEFAULT_GROUP = "ato-reasoner"
#: This instance's name within the group (unique per running consumer).
DEFAULT_CONSUMER = "consumer-0"
#: Events older than this many seconds are dead-lettered (one hour).
DEFAULT_LATENESS_SECONDS = 3_600


class IngestionConsumer:
    """Validates and deduplicates login events from a Redis Streams consumer group.

    Multiple IngestionConsumer instances sharing the same ``group`` will
    each receive a distinct subset of messages. Each message is ACKed
    after processing regardless of outcome — failures go to the DLQ
    rather than being redelivered.

    Args:
        client: Connected Redis client (decode_responses=True).
        stream_key: Redis Streams key to read from.
        group: Consumer group name.
        consumer: Consumer instance name. Must be unique per running instance.
        lateness_seconds: Events older than this number of seconds are
            dead-lettered. Defaults to 3600 (1 hour).
        dedup_ttl_seconds: IdempotencyStore key TTL. Defaults to 86400 (24h).
    """

    def __init__(
        self,
        client: redis_lib.Redis,
        *,
        stream_key: str = DEFAULT_STREAM_KEY,
        group: str = DEFAULT_GROUP,
        consumer: str = DEFAULT_CONSUMER,
        lateness_seconds: int = DEFAULT_LATENESS_SECONDS,
        dedup_ttl_seconds: int = 86_400,
    ) -> None:
        """Initialize the consumer with a Redis client and configuration."""
        self._client = client
        self._stream_key = stream_key
        self._dlq_key = f"{stream_key}:dlq"
        self._group = group
        self._consumer = consumer
        self._lateness = timedelta(seconds=lateness_seconds)
        self._dedup = IdempotencyStore(client, ttl_seconds=dedup_ttl_seconds)

    def ensure_group(self) -> None:
        """Create the consumer group if it does not already exist.

        Uses ``MKSTREAM`` so the stream is created if absent. Safe to
        call on every startup — ``BUSYGROUP`` errors are suppressed.
        """
        try:
            self._client.xgroup_create(
                self._stream_key, self._group, id="0", mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def process_message(self, message_id: str, raw_data: str) -> LoginEvent | None:
        """Validate, deduplicate, and ACK a single stream message.

        Applies three checks in order: schema validation, bounded lateness,
        deduplication. The first failure routes the message to the DLQ and
        ACKs it. On success, the message is ACKed and the LoginEvent returned.

        Args:
            message_id: Redis Streams message ID (for ACK and DLQ provenance).
            raw_data: Raw JSON string from the ``"data"`` field of the entry.

        Returns:
            Parsed LoginEvent on success, None on any failure.
        """
        # 1. Schema validation
        try:
            event = LoginEvent.model_validate_json(raw_data)
        except (ValidationError, ValueError) as exc:
            self.dead_letter(
                message_id,
                raw_data,
                reason="schema_error",
                detail=str(exc),
            )
            self._ack(message_id)
            return None

        # 2. Bounded lateness
        age = datetime.now(UTC) - event.timestamp
        if age > self._lateness:
            self.dead_letter(
                message_id,
                raw_data,
                reason="lateness",
                detail=(
                    f"event age {age.total_seconds():.1f}s exceeds "
                    f"limit {self._lateness.total_seconds():.1f}s"
                ),
                event_id=event.event_id,
            )
            self._ack(message_id)
            log.warning(
                "event.late",
                component="ingestion",
                event_id=event.event_id,
                age_seconds=round(age.total_seconds(), 1),
            )
            return None

        # 3. Deduplication
        if not self._dedup.is_new(event.event_id):
            self.dead_letter(
                message_id,
                raw_data,
                reason="duplicate",
                detail=(
                    f"event_id {event.event_id!r} already processed within TTL window"
                ),
                event_id=event.event_id,
            )
            self._ack(message_id)
            log.debug(
                "event.duplicate",
                component="ingestion",
                event_id=event.event_id,
            )
            return None

        self._ack(message_id)
        log.debug(
            "event.accepted",
            component="ingestion",
            event_id=event.event_id,
            account_id=event.account_id,
        )
        return event.model_copy(update={"scenario_tag": None})

    def consume(
        self,
        handler: Callable[[LoginEvent], None],
        *,
        batch_size: int = 10,
        block_ms: int = 1_000,
        max_batches: int | None = None,
    ) -> int:
        """Run the consume loop, calling handler for each valid accepted event.

        Calls ``ensure_group()`` on entry. Blocks up to ``block_ms``
        milliseconds when the stream is empty. Returns when ``max_batches``
        XREADGROUP calls have been made, or runs indefinitely when
        ``max_batches`` is None.

        Args:
            handler: Called once per accepted LoginEvent.
            batch_size: Maximum messages per XREADGROUP call.
            block_ms: Milliseconds to block waiting for new messages.
            max_batches: Stop after this many XREADGROUP calls. Pass a
                small value (e.g. 1) in tests to avoid blocking forever.

        Returns:
            Total number of valid events delivered to handler.
        """
        self.ensure_group()
        accepted = 0
        batches = 0

        while max_batches is None or batches < max_batches:
            messages = self.read(batch_size=batch_size, block_ms=block_ms)
            batches += 1
            for message_id, raw in messages:
                event = self.process_message(message_id, raw)
                if event is not None:
                    handler(event)
                    accepted += 1

        return accepted

    def read(
        self, *, batch_size: int = 10, block_ms: int = 1_000, pending: bool = False
    ) -> list[tuple[str, str]]:
        """Read messages delivered to this consumer (one XREADGROUP).

        By default, reads new messages: reading delivers them to this
        consumer, and each stays pending in the group until
        ``process_message`` ACKs it. With ``pending``, re-reads this
        consumer's own backlog instead: messages delivered to it earlier and
        never ACKed (a run that stopped after reading them), oldest first,
        without blocking. Nothing is validated here.

        Args:
            batch_size: Maximum messages to read.
            block_ms: Milliseconds to block waiting for new messages
                (ignored with ``pending``).
            pending: Re-read this consumer's unacknowledged messages.

        Returns:
            ``(message_id, data)`` per message, in stream order; empty when
            nothing arrived within ``block_ms`` (or, with ``pending``, when
            the backlog is empty). ``data`` is the entry's ``"data"`` field
            (empty when the entry has none, or was deleted from the stream
            while pending).
        """
        results = self._client.xreadgroup(
            self._group,
            self._consumer,
            {self._stream_key: "0" if pending else ">"},
            count=batch_size,
            block=None if pending else block_ms,
        )
        return [
            (message_id, (fields or {}).get("data", ""))
            for _stream, messages in results or []  # type: ignore[union-attr]
            for message_id, fields in messages
        ]

    def dead_letter(
        self,
        message_id: str,
        raw_data: str,
        *,
        reason: str,
        detail: str,
        event_id: str | None = None,
    ) -> None:
        """Copy a message to the dead-letter stream with the reason it failed.

        Does not ACK: ``process_message`` ACKs every message it checks, and
        a caller that dead-letters an accepted event after a failed decision
        does so after that ACK.

        Args:
            message_id: The message's Redis Streams ID (``original_id``).
            raw_data: The message's original ``"data"`` payload.
            reason: Why it failed (see the module docstring).
            detail: Human-readable description of the failure.
            event_id: The parsed event id, when there is one.
        """
        entry: dict[str, str] = {
            "data": raw_data,
            "reason": reason,
            "detail": detail,
            "original_id": message_id,
        }
        if event_id is not None:
            entry["event_id"] = event_id
        self._client.xadd(self._dlq_key, entry)  # type: ignore[union-attr, arg-type]
        log.warning(
            "event.dead_lettered",
            component="ingestion",
            reason=reason,
            original_id=message_id,
            event_id=event_id,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ack(self, message_id: str) -> None:
        self._client.xack(self._stream_key, self._group, message_id)
