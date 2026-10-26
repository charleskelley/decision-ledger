"""The settlement row: what it refuses, its wire form, and the subject it names.

An ``AccountSettlement`` is strict, frozen, and closed: no field it does not
define, no coercion from another type, no change once built. A target is
always positive and an actual never negative (nothing settled is a row), a
reconciliation is taken at a timezone-aware time, and a row holds nothing
the ledger could not store. It travels as JSON (``payload``, without its
derived id) and is read back strictly through JSON (``from_payload``).
Every day's row for one account names the same ledger subject: the UUID5 of
its ``account_id`` under the namespace the registration declares, as the
native subject ``(mock-daily-account, account_id)``.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from core.identity import NativeSubject
from mock_daily_reasoner.registry import MOCK_DAILY_ACCOUNT_NAMESPACE
from mock_daily_reasoner.settlements import AccountSettlement

#: Every field of a row: what the extract carries.
FIELDS = frozenset(
    {"event_id", "as_of", "account_id", "target_amount", "actual_amount"}
)


def _fields(row, **overrides):
    """A row's fields as Python values, with ``overrides`` applied."""
    return {**{name: getattr(row, name) for name in FIELDS}, **overrides}


def test_a_row_cannot_change_once_built(settlement):
    row = settlement(1, actual=990.0)

    with pytest.raises(ValidationError, match="frozen"):
        row.actual_amount = 1_000.0

    assert row.actual_amount == 990.0


def test_a_row_refuses_a_field_it_does_not_define(settlement):
    with pytest.raises(ValidationError) as err:
        AccountSettlement(**_fields(settlement(1), currency="EUR"))

    assert [(e["type"], e["loc"]) for e in err.value.errors()] == [
        ("extra_forbidden", ("currency",))
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("target_amount", "1000.0", id="a-target-as-text"),
        pytest.param("actual_amount", True, id="an-actual-as-a-bool"),
        pytest.param("as_of", "2026-10-22T23:59:59Z", id="as-of-as-text"),
        pytest.param("account_id", 1, id="an-account-id-as-a-number"),
    ],
)
def test_a_row_refuses_a_value_of_another_type_rather_than_coerce_it(
    settlement, field, value
):
    with pytest.raises(ValidationError) as err:
        AccountSettlement(**_fields(settlement(1), **{field: value}))

    assert {e["loc"][0] for e in err.value.errors()} == {field}


def test_a_row_refuses_a_reconciliation_taken_without_a_time_zone(settlement):
    naive = datetime(2026, 10, 22, 23, 59, 59)  # no tzinfo: the point

    with pytest.raises(ValidationError, match="timezone"):
        AccountSettlement(**_fields(settlement(1), as_of=naive))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("target_amount", 0.0, id="a-zero-target"),
        pytest.param("target_amount", -500.0, id="a-negative-target"),
        pytest.param("actual_amount", -0.01, id="a-negative-actual"),
        pytest.param("event_id", "", id="a-blank-event-id"),
        pytest.param("account_id", "", id="a-blank-account-id"),
    ],
)
def test_a_row_refuses_a_value_outside_its_range(settlement, field, value):
    with pytest.raises(ValidationError) as err:
        AccountSettlement(**_fields(settlement(1), **{field: value}))

    assert {e["loc"][0] for e in err.value.errors()} == {field}


def test_an_account_that_settled_nothing_is_a_row(settlement):
    row = settlement(1, target=1_000.0, actual=0.0)

    assert row.actual_amount == 0.0


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        pytest.param(
            {"account_id": "acct\x00001"},
            "NUL character, which the ledger cannot store",
            id="a-nul-character",
        ),
        pytest.param(
            {"event_id": "set-\ud800"}, "unicode string", id="a-lone-surrogate"
        ),
        pytest.param({"actual_amount": float("nan")}, "finite number", id="nan"),
        pytest.param({"target_amount": float("inf")}, "finite number", id="infinity"),
    ],
)
def test_a_row_refuses_a_value_the_ledger_cannot_store(settlement, overrides, why):
    with pytest.raises(ValidationError, match=why):
        AccountSettlement(**_fields(settlement(1), **overrides))


def test_the_payload_is_the_row_as_json_without_its_derived_id(settlement):
    row = settlement(1, target=2_500.0, actual=2_490.0)

    payload = row.payload()

    assert set(payload) == FIELDS
    assert payload["as_of"] == "2026-10-22T23:59:59Z"
    assert json.loads(json.dumps(payload)) == payload
    assert AccountSettlement.from_payload(payload) == row
    assert AccountSettlement.from_payload(json.loads(json.dumps(payload))) == row


def test_from_payload_takes_a_json_integer_amount_as_a_number(settlement):
    payload = {**settlement(1).payload(), "target_amount": 2500, "actual_amount": 0}

    row = AccountSettlement.from_payload(payload)

    assert (row.target_amount, row.actual_amount) == (2_500.0, 0.0)
    assert type(row.target_amount) is float
    assert type(row.actual_amount) is float


def test_from_payload_refuses_the_derived_id(settlement):
    row = settlement(1)
    payload = {**row.payload(), "entity_id": str(row.entity_id)}

    with pytest.raises(ValidationError) as err:
        AccountSettlement.from_payload(payload)

    assert [(e["type"], e["loc"]) for e in err.value.errors()] == [
        ("extra_forbidden", ("entity_id",))
    ]


def test_from_payload_refuses_a_payload_that_is_not_json(settlement):
    with pytest.raises(TypeError, match="not JSON serializable"):
        AccountSettlement.from_payload(
            {**settlement(1).payload(), "actual_amount": {1_000.0}}
        )


def test_every_days_row_for_an_account_names_one_ledger_subject(settlement, as_of):
    today = settlement(1, account="acct-a")
    tomorrow = settlement(2, account="acct-a", as_of=as_of + timedelta(days=1))
    other = settlement(3, account="acct-b")

    expected = uuid.uuid5(MOCK_DAILY_ACCOUNT_NAMESPACE.namespace_uuid, "acct-a")
    assert today.entity_id == tomorrow.entity_id == expected
    assert MOCK_DAILY_ACCOUNT_NAMESPACE.entity_id_of("acct-a") == expected
    assert other.entity_id == uuid.uuid5(
        MOCK_DAILY_ACCOUNT_NAMESPACE.namespace_uuid, "acct-b"
    )
    assert other.entity_id != expected


def test_the_account_is_the_native_subject_in_the_declared_namespace(settlement):
    row = settlement(1, account="acct-a")

    assert row.native_subject == NativeSubject(
        native_namespace="mock-daily-account", native_subject_id="acct-a"
    )
    assert row.entity_type == "account"
    assert MOCK_DAILY_ACCOUNT_NAMESPACE.native_namespace == "mock-daily-account"
    assert MOCK_DAILY_ACCOUNT_NAMESPACE.entity_type == row.entity_type
