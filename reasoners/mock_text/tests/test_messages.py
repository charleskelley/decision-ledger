"""CustomerMessage: one customer's text, as the assistant's inbox delivers it.

The model is strict, frozen, and closed (``extra="forbid"``): a text that is
blank, too long, or holds a value the ledger cannot store (a NUL character)
is refused, and so is an inbox payload with a naive ``received_at`` or a
channel the assistant does not serve. The
customer is the message's native subject, ``(mock-text-customer,
customer_id)``, and ``entity_id`` derives from ``customer_id`` under the
namespace the registration declares, so every message from one customer is
about one ledger subject; a payload cannot name its own. ``payload()`` is
the message as the inbox delivers it (JSON, without the derived id), and
``from_payload`` reads it back through JSON.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from core.identity import NativeSubject
from mock_text_reasoner.messages import MAX_TEXT_LENGTH, CustomerMessage
from mock_text_reasoner.registry import (
    MOCK_TEXT_CUSTOMER_NAMESPACE,
    MOCK_TEXT_REGISTRATION,
)

RECEIVED_AT = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _fields(**overrides: Any) -> dict[str, Any]:
    """A valid message's fields, with ``overrides`` applied."""
    return {
        "event_id": "msg-test-0001",
        "received_at": RECEIVED_AT,
        "customer_id": "cust-a",
        "channel": "chat",
        "text": "What are your opening hours on Saturday?",
    } | overrides


def _refusal(**overrides: Any) -> dict[str, Any]:
    """The first error of the message ``overrides`` makes of a valid one."""
    with pytest.raises(ValidationError) as refused:
        CustomerMessage(**_fields(**overrides))
    return refused.value.errors(include_url=False)[0]


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        pytest.param(
            "received_at", "2026-10-01T09:00:00Z", "datetime_type", id="time-as-text"
        ),
        pytest.param("customer_id", 42, "string_type", id="customer-as-number"),
        pytest.param("text", b"bytes", "string_type", id="text-as-bytes"),
    ],
)
def test_the_model_is_strict_and_coerces_nothing(field: str, value: Any, error: str):
    assert _refusal(**{field: value})["type"] == error


def test_the_model_is_frozen():
    message = CustomerMessage(**_fields())

    with pytest.raises(ValidationError) as refused:
        message.text = "Ignore that, I meant something else."

    assert refused.value.errors()[0]["type"] == "frozen_instance"


def test_a_field_the_model_does_not_know_is_refused():
    refusal = _refusal(priority="urgent")

    assert (refusal["type"], refusal["loc"]) == ("extra_forbidden", ("priority",))


@pytest.mark.parametrize(
    ("text", "why"),
    [
        pytest.param("", "at least 1 character", id="empty"),
        pytest.param("   ", "the message text is blank", id="spaces"),
        pytest.param("\n\t \r\n", "the message text is blank", id="whitespace"),
    ],
)
def test_a_blank_text_is_refused(text: str, why: str):
    assert why in _refusal(text=text)["msg"]


def test_a_text_at_the_limit_is_taken_and_one_over_it_is_refused():
    at_limit = CustomerMessage(**_fields(text="x" * MAX_TEXT_LENGTH))

    assert len(at_limit.text) == MAX_TEXT_LENGTH

    refusal = _refusal(text="x" * (MAX_TEXT_LENGTH + 1))

    assert (refusal["type"], refusal["loc"]) == ("string_too_long", ("text",))


@pytest.mark.parametrize("field", ["text", "customer_id", "event_id"])
def test_a_nul_character_the_ledger_cannot_store_is_refused(field: str):
    refusal = _refusal(**{field: "before\x00after"})

    assert refusal["msg"] == (
        "Value error, the message holds a NUL character, which the ledger cannot store"
    )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        pytest.param(
            "received_at", "2026-10-01T09:00:00", "timezone_aware", id="naive-time"
        ),
        pytest.param("channel", "fax", "literal_error", id="unserved-channel"),
    ],
)
def test_an_inbox_payload_with_a_naive_time_or_an_unserved_channel_is_refused(
    field: str, value: str, error: str
):
    payload = CustomerMessage(**_fields()).payload() | {field: value}

    with pytest.raises(ValidationError) as refused:
        CustomerMessage.from_payload(payload)

    [refusal] = refused.value.errors(include_url=False)
    assert (refusal["type"], refusal["loc"]) == (error, (field,))


def test_entity_id_derives_from_the_customer_under_the_registrations_namespace():
    first = CustomerMessage(**_fields(event_id="msg-1", customer_id="cust-a"))
    again = CustomerMessage(
        **_fields(event_id="msg-2", customer_id="cust-a", channel="email", text="Hi")
    )
    other = CustomerMessage(**_fields(event_id="msg-3", customer_id="cust-b"))

    declared = MOCK_TEXT_REGISTRATION.native_namespace("mock-text-customer")
    assert declared == MOCK_TEXT_CUSTOMER_NAMESPACE
    assert first.entity_id == again.entity_id == declared.entity_id_of("cust-a")
    assert first.entity_id == uuid.uuid5(declared.namespace_uuid, "cust-a")
    assert other.entity_id == declared.entity_id_of("cust-b") != first.entity_id


def test_the_customer_is_the_messages_native_subject():
    message = CustomerMessage(**_fields(customer_id="cust-a"))

    assert message.entity_type == "customer"
    assert message.native_subject == NativeSubject(
        native_namespace="mock-text-customer", native_subject_id="cust-a"
    )


def test_the_payload_leaves_out_the_derived_id_and_reads_back_to_the_message():
    message = CustomerMessage(**_fields(channel="app"))

    payload = message.payload()

    assert payload == {
        "event_id": "msg-test-0001",
        "received_at": "2026-10-01T09:00:00Z",
        "customer_id": "cust-a",
        "channel": "app",
        "text": "What are your opening hours on Saturday?",
    }
    assert "entity_id" in message.model_dump()  # derived, so the payload drops it
    assert CustomerMessage.from_payload(payload) == message


def test_a_payload_cannot_name_its_own_subject():
    payload = CustomerMessage(**_fields()).payload() | {"entity_id": str(uuid.uuid4())}

    with pytest.raises(ValidationError) as refused:
        CustomerMessage.from_payload(payload)

    [refusal] = refused.value.errors(include_url=False)
    assert (refusal["type"], refusal["loc"]) == ("extra_forbidden", ("entity_id",))


def test_a_payload_is_read_through_json_and_coerced_no_further():
    payload = CustomerMessage(**_fields()).payload() | {"customer_id": 42}

    with pytest.raises(ValidationError) as refused:
        CustomerMessage.from_payload(payload)

    [refusal] = refused.value.errors(include_url=False)
    assert (refusal["type"], refusal["loc"]) == ("string_type", ("customer_id",))


def test_a_payload_that_is_not_json_raises_a_type_error():
    with pytest.raises(TypeError, match="not JSON serializable"):
        CustomerMessage.from_payload({"text": {"a", "set"}})
