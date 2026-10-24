"""The batch's row: what it refuses, its wire form, and the subject it names.

An ``AccountDay`` is strict, frozen, and closed: no field it does not
define, no coercion from another type, no change once built. A balance is
never negative and a credit limit always positive, and a row holds nothing
the ledger could not store. It travels as JSON (``payload``, without its
derived id) and is read back strictly through JSON (``from_payload``).
Every day's row for one account names the same ledger subject: the UUID5 of
its ``account_id`` under the namespace the registration declares, as the
native subject ``(mock-batch-account, account_id)``.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from core.identity import NativeSubject
from mock_batch_reasoner.registry import MOCK_BATCH_ACCOUNT_NAMESPACE
from mock_batch_reasoner.rows import AccountDay

#: Every field of a row: what the extract carries.
FIELDS = frozenset(
    {
        "event_id",
        "as_of",
        "account_id",
        "balance",
        "credit_limit",
        "days_past_due",
        "returned_payments",
        "needs_review",
    }
)


def _fields(position, **overrides):
    """A row's fields as Python values, with ``overrides`` applied."""
    return {**{name: getattr(position, name) for name in FIELDS}, **overrides}


def test_a_row_cannot_change_once_built(row):
    position = row(1)

    with pytest.raises(ValidationError, match="frozen"):
        position.balance = 900.0

    assert position.balance == 250.0


def test_a_row_refuses_a_field_it_does_not_define(row):
    with pytest.raises(ValidationError) as err:
        AccountDay(**_fields(row(1), branch="north"))

    assert [(e["type"], e["loc"]) for e in err.value.errors()] == [
        ("extra_forbidden", ("branch",))
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("balance", "250.0", id="a-balance-as-text"),
        pytest.param("days_past_due", 30.0, id="days-as-a-float"),
        pytest.param("returned_payments", True, id="a-count-as-a-bool"),
        pytest.param("needs_review", 1, id="the-flag-as-a-number"),
        pytest.param("as_of", "2026-10-22T23:59:59Z", id="as-of-as-text"),
    ],
)
def test_a_row_refuses_a_value_of_another_type_rather_than_coerce_it(row, field, value):
    with pytest.raises(ValidationError) as err:
        AccountDay(**_fields(row(1), **{field: value}))

    assert {e["loc"][0] for e in err.value.errors()} == {field}


def test_a_row_refuses_a_position_taken_without_a_time_zone(row):
    naive = datetime(2026, 10, 22, 23, 59, 59)  # no tzinfo: the point

    with pytest.raises(ValidationError, match="timezone"):
        AccountDay(**_fields(row(1), as_of=naive))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("balance", -0.01, id="a-negative-balance"),
        pytest.param("credit_limit", 0.0, id="a-zero-credit-limit"),
        pytest.param("credit_limit", -500.0, id="a-negative-credit-limit"),
        pytest.param("days_past_due", -1, id="negative-days-past-due"),
        pytest.param("returned_payments", -1, id="negative-returned-payments"),
        pytest.param("event_id", "", id="a-blank-event-id"),
        pytest.param("account_id", "", id="a-blank-account-id"),
    ],
)
def test_a_row_refuses_a_value_outside_its_range(row, field, value):
    with pytest.raises(ValidationError) as err:
        AccountDay(**_fields(row(1), **{field: value}))

    assert {e["loc"][0] for e in err.value.errors()} == {field}


def test_an_account_that_owes_nothing_is_a_row(row):
    position = row(1, balance=0.0)

    assert position.balance == 0.0


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        pytest.param(
            {"account_id": "acct\x00001"},
            "NUL character, which the ledger cannot store",
            id="a-nul-character",
        ),
        pytest.param(
            {"event_id": "row-\ud800"}, "unicode string", id="a-lone-surrogate"
        ),
        pytest.param({"balance": float("nan")}, "finite number", id="nan"),
        pytest.param({"credit_limit": float("inf")}, "finite number", id="infinity"),
    ],
)
def test_a_row_refuses_a_value_the_ledger_cannot_store(row, overrides, why):
    with pytest.raises(ValidationError, match=why):
        AccountDay(**_fields(row(1), **overrides))


def test_the_payload_is_the_row_as_json_without_its_derived_id(row):
    position = row(1, balance=640.5, days_past_due=30, needs_review=True)

    payload = position.payload()

    assert set(payload) == FIELDS
    assert payload["as_of"] == "2026-10-22T23:59:59Z"
    assert json.loads(json.dumps(payload)) == payload
    assert AccountDay.from_payload(payload) == position
    assert AccountDay.from_payload(json.loads(json.dumps(payload))) == position


def test_from_payload_takes_a_json_integer_amount_as_a_number(row):
    payload = {**row(1).payload(), "balance": 250, "credit_limit": 1000}

    position = AccountDay.from_payload(payload)

    assert (position.balance, position.credit_limit) == (250.0, 1000.0)
    assert type(position.balance) is float
    assert type(position.credit_limit) is float


def test_from_payload_refuses_the_derived_id(row):
    position = row(1)
    payload = {**position.payload(), "entity_id": str(position.entity_id)}

    with pytest.raises(ValidationError) as err:
        AccountDay.from_payload(payload)

    assert [(e["type"], e["loc"]) for e in err.value.errors()] == [
        ("extra_forbidden", ("entity_id",))
    ]


def test_from_payload_refuses_a_payload_that_is_not_json(row):
    with pytest.raises(TypeError, match="not JSON serializable"):
        AccountDay.from_payload({**row(1).payload(), "balance": {250.0}})


def test_every_days_row_for_an_account_names_one_ledger_subject(row, as_of):
    today = row(1, account="acct-a")
    tomorrow = AccountDay(
        **_fields(row(2, account="acct-a"), as_of=as_of + timedelta(days=1))
    )
    other = row(3, account="acct-b")

    expected = uuid.uuid5(MOCK_BATCH_ACCOUNT_NAMESPACE.namespace_uuid, "acct-a")
    assert today.entity_id == tomorrow.entity_id == expected
    assert MOCK_BATCH_ACCOUNT_NAMESPACE.entity_id_of("acct-a") == expected
    assert other.entity_id == uuid.uuid5(
        MOCK_BATCH_ACCOUNT_NAMESPACE.namespace_uuid, "acct-b"
    )
    assert other.entity_id != expected


def test_the_account_is_the_native_subject_in_the_declared_namespace(row):
    position = row(1, account="acct-a")

    assert position.native_subject == NativeSubject(
        native_namespace="mock-batch-account", native_subject_id="acct-a"
    )
    assert position.entity_type == "account"
    assert MOCK_BATCH_ACCOUNT_NAMESPACE.native_namespace == "mock-batch-account"
    assert MOCK_BATCH_ACCOUNT_NAMESPACE.entity_type == position.entity_type
