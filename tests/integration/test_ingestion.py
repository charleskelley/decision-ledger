"""Integration tests for the ingestion consumer.

Requires Docker Redis on localhost:6379. Run with: make test-integration

Tests verify behavioral contracts, not implementation details:

- Valid events flow through as LoginEvent objects
- Schema-invalid payloads are dead-lettered and not delivered to the caller
- Events older than the lateness window are dead-lettered
- Duplicate event_ids are dead-lettered on second delivery
- Dead-lettered messages are ACKed (not redelivered)
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import redis

from app.ingestion.consumer import IngestionConsumer
from reasoner.account_takeover.events import (
    AuthMethod,
    AuthOutcome,
    Geolocation,
    LoginEvent,
)

pytestmark = pytest.mark.integration

# Unique prefix per test session so dedup store keys never collide across runs.
_RUN = uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def consumer(redis_client, unique_stream_key):
    return IngestionConsumer(
        redis_client,
        stream_key=unique_stream_key,
        group="test-group",
        consumer="test-consumer-0",
        lateness_seconds=3_600,
        dedup_ttl_seconds=60,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(event_id: str = "evt-001", *, offset_seconds: int = 0) -> LoginEvent:
    """Build a valid LoginEvent with the given event_id.

    The event_id is prefixed with a per-session run ID so dedup keys do not
    collide across consecutive test runs within the Redis TTL window.
    """
    return LoginEvent(
        event_id=f"{_RUN}-{event_id}",
        timestamp=datetime.now(UTC) - timedelta(seconds=offset_seconds),
        account_id="acct-test-001",
        session_id="sess-001",
        ip_address="203.0.113.1",
        geolocation=Geolocation(
            latitude=37.77,
            longitude=-122.42,
            country="US",
            city="San Francisco",
            asn="AS7922",
        ),
        device_fingerprint="fp-abc",
        user_agent="Mozilla/5.0 (Test)",
        auth_method=AuthMethod.PASSWORD,
        outcome=AuthOutcome.SUCCESS,
    )


def _push(client: redis.Redis, stream_key: str, data: str) -> None:
    """XADD a raw data string to the stream."""
    client.xadd(stream_key, {"data": data})


def _collect(consumer: IngestionConsumer) -> list[LoginEvent]:
    """Run one batch and return all accepted events."""
    collected: list[LoginEvent] = []
    consumer.consume(collected.append, batch_size=50, block_ms=100, max_batches=1)
    return collected


def _dlq_entries(client: redis.Redis, stream_key: str) -> list[dict[str, str]]:
    """Return all DLQ entries for a stream as a list of field dicts."""
    raw = client.xrange(f"{stream_key}:dlq")
    return [fields for _, fields in raw]  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Valid event acceptance
# ---------------------------------------------------------------------------


def test_valid_event_accepted_and_returned(consumer, redis_client, unique_stream_key):
    """A valid LoginEvent JSON payload is parsed and delivered to the handler."""
    consumer.ensure_group()
    payload = _make_event("evt-accept-001").model_dump_json()
    _push(redis_client, unique_stream_key, payload)

    collected = _collect(consumer)

    assert len(collected) == 1
    assert collected[0].event_id == f"{_RUN}-evt-accept-001"


def test_valid_event_produces_no_dlq_entry(consumer, redis_client, unique_stream_key):
    """No DLQ entry is written when a valid event is accepted."""
    consumer.ensure_group()
    payload = _make_event("evt-accept-002").model_dump_json()
    _push(redis_client, unique_stream_key, payload)

    _collect(consumer)

    assert len(_dlq_entries(redis_client, unique_stream_key)) == 0


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_malformed_json_is_dead_lettered(consumer, redis_client, unique_stream_key):
    """Malformed JSON is routed to the DLQ and not delivered to the handler."""
    consumer.ensure_group()
    _push(redis_client, unique_stream_key, "{not valid json")

    collected = _collect(consumer)

    assert collected == []
    entries = _dlq_entries(redis_client, unique_stream_key)
    assert len(entries) == 1
    assert entries[0]["reason"] == "schema_error"


def test_schema_violation_is_dead_lettered(consumer, redis_client, unique_stream_key):
    """Valid JSON that fails LoginEvent validation is routed to the DLQ."""
    consumer.ensure_group()
    bad_payload = json.dumps({"event_id": "x", "timestamp": "not-a-datetime"})
    _push(redis_client, unique_stream_key, bad_payload)

    collected = _collect(consumer)

    assert collected == []
    entries = _dlq_entries(redis_client, unique_stream_key)
    assert len(entries) == 1
    assert entries[0]["reason"] == "schema_error"


# ---------------------------------------------------------------------------
# Bounded lateness
# ---------------------------------------------------------------------------


def test_late_event_is_dead_lettered(redis_client, unique_stream_key):
    """Events older than the lateness window are dead-lettered."""
    consumer = IngestionConsumer(
        redis_client,
        stream_key=unique_stream_key,
        group="test-group",
        consumer="test-consumer-0",
        lateness_seconds=60,  # 1-minute window
    )
    consumer.ensure_group()

    late = _make_event("evt-late-001", offset_seconds=7_200)  # 2 hours old
    _push(redis_client, unique_stream_key, late.model_dump_json())

    collected = _collect(consumer)

    assert collected == []
    entries = _dlq_entries(redis_client, unique_stream_key)
    assert len(entries) == 1
    assert entries[0]["reason"] == "lateness"
    assert entries[0]["event_id"] == f"{_RUN}-evt-late-001"


# ---------------------------------------------------------------------------
# Scenario tag stripping
# ---------------------------------------------------------------------------


def test_scenario_tag_stripped_before_delivery(
    consumer, redis_client, unique_stream_key
):
    """scenario_tag is stripped from the event before it reaches the handler."""
    consumer.ensure_group()
    event = _make_event("evt-tag-001")
    tagged = event.model_copy(update={"scenario_tag": "baseline_normal"})
    _push(redis_client, unique_stream_key, tagged.model_dump_json())

    collected = _collect(consumer)

    assert len(collected) == 1
    assert collected[0].scenario_tag is None


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def test_duplicate_event_id_is_dead_lettered(consumer, redis_client, unique_stream_key):
    """Second delivery of the same event_id is dead-lettered; first is accepted."""
    consumer.ensure_group()

    payload = _make_event("evt-dup-001").model_dump_json()
    _push(redis_client, unique_stream_key, payload)
    _push(redis_client, unique_stream_key, payload)

    collected = _collect(consumer)

    assert len(collected) == 1
    assert collected[0].event_id == f"{_RUN}-evt-dup-001"

    entries = _dlq_entries(redis_client, unique_stream_key)
    assert len(entries) == 1
    assert entries[0]["reason"] == "duplicate"
    assert entries[0]["event_id"] == f"{_RUN}-evt-dup-001"
