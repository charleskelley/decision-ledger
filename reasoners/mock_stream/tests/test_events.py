"""The stream's event: its wire form, what it refuses, and the subject it names.

A ``TransferEvent`` travels as JSON (``payload``) and is read back strictly
through JSON (``from_payload``): an aware ISO 8601 timestamp, a positive
amount, no derived key, and nothing the ledger could not store. Every
transfer of one account names the same ledger subject: the UUID5 of its
``account_id`` under the namespace the registration declares.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from core.identity import NativeSubject
from mock_stream_reasoner.events import TransferEvent
from mock_stream_reasoner.registry import MOCK_STREAM_ACCOUNT_NAMESPACE


def _payload(**overrides):
    payload = {
        "event_id": "evt-0001",
        "timestamp": "2026-10-01T09:00:00Z",
        "account_id": "acct-001",
        "amount": 120.5,
    }
    payload.update(overrides)
    return payload


def test_the_stream_payload_round_trips_through_from_payload(transfer):
    event = transfer(1, "acct-001", 120.5, minutes=3)

    payload = event.payload()

    assert set(payload) == {"event_id", "timestamp", "account_id", "amount"}
    assert TransferEvent.from_payload(payload) == event
    assert TransferEvent.from_payload(json.loads(json.dumps(payload))) == event


@pytest.mark.parametrize(
    "timestamp", ["2026-10-01T09:00:00Z", "2026-10-01T11:00:00+02:00"]
)
def test_from_payload_reads_an_iso_8601_timestamp_with_its_offset(timestamp):
    event = TransferEvent.from_payload(_payload(timestamp=timestamp))

    assert event.timestamp == datetime(2026, 10, 1, 9, tzinfo=UTC)
    assert event.timestamp.utcoffset() is not None


def test_from_payload_takes_a_json_integer_amount_as_a_number():
    event = TransferEvent.from_payload(_payload(amount=120))

    assert event.amount == 120.0
    assert type(event.amount) is float


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        pytest.param({"timestamp": "2026-10-01T09:00:00"}, "timestamp", id="naive"),
        pytest.param({"timestamp": "2026-10-01"}, "timestamp", id="a-date-only"),
        pytest.param({"amount": 0}, "amount", id="zero-amount"),
        pytest.param({"amount": -25.0}, "amount", id="negative-amount"),
        pytest.param({"amount": "120.5"}, "amount", id="amount-as-text"),
        pytest.param({"amount": float("nan")}, "amount", id="nan-amount"),
        pytest.param({"event_id": ""}, "event_id", id="blank-event-id"),
        pytest.param(
            {"entity_id": "8c6f0a8e-4a8f-5d7e-9a0b-0c1d2e3f4a5b"},
            "entity_id",
            id="the-derived-id",
        ),
    ],
)
def test_from_payload_refuses_a_transfer_that_is_not_one(overrides, field):
    with pytest.raises(ValidationError) as err:
        TransferEvent.from_payload(_payload(**overrides))

    assert field in {str(e["loc"][0]) for e in err.value.errors() if e["loc"]}


def test_from_payload_refuses_a_value_the_ledger_cannot_store():
    with pytest.raises(ValidationError, match="NUL character"):
        TransferEvent.from_payload(_payload(account_id="acct\x00001"))


def test_from_payload_refuses_a_payload_that_is_not_json():
    with pytest.raises(TypeError, match="not JSON serializable"):
        TransferEvent.from_payload(_payload(amount={120.5}))


def test_every_transfer_of_an_account_names_one_ledger_subject(transfer):
    first = transfer(1, "acct-001", 50.0)
    later = transfer(2, "acct-001", 900.0, minutes=30)
    other = transfer(3, "acct-002", 50.0)

    expected = uuid.uuid5(MOCK_STREAM_ACCOUNT_NAMESPACE.namespace_uuid, "acct-001")
    assert first.entity_id == later.entity_id == expected
    assert other.entity_id == uuid.uuid5(
        MOCK_STREAM_ACCOUNT_NAMESPACE.namespace_uuid, "acct-002"
    )
    assert other.entity_id != expected


def test_the_account_is_the_native_subject_in_the_declared_namespace(transfer):
    event = transfer(1, "acct-001", 50.0)

    assert event.native_subject == NativeSubject(
        native_namespace="mock-stream-account", native_subject_id="acct-001"
    )
    assert event.entity_type == "account"
    assert MOCK_STREAM_ACCOUNT_NAMESPACE.native_namespace == "mock-stream-account"
    assert MOCK_STREAM_ACCOUNT_NAMESPACE.entity_type == event.entity_type
