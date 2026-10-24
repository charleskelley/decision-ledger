"""The synthetic day: a deterministic overnight extract that covers every band.

``synthetic_day`` gives the same rows for the same arguments and other rows
for another seed. Row ids carry the run tag, so two runs never share an
``event_id``; account ids do not, so every day names the same accounts. Its
first five rows are one of each profile: recorded under ``current-account``,
``watch-list``, ``delinquent-hold``, and ``charge-off``, then one exception;
every unflagged row is recorded and every flagged row is an exception. A
day below ``MIN_COUNT`` rows is refused. A default day (25,000 rows, seed
7) plans three batch requests of at most 10,000 lines.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from mock_batch_reasoner.batch import MAX_BATCH_LINES, plan_day, score_rows
from mock_batch_reasoner.synthetic import (
    CUT_OFF,
    DEFAULT_COUNT,
    DEFAULT_SEED,
    MIN_COUNT,
    synthetic_day,
)


def _without_id(position):
    """A row's payload without its id: the account's position."""
    return {k: v for k, v in position.payload().items() if k != "event_id"}


def test_the_same_arguments_give_the_same_day_and_another_seed_another(
    business_date,
):
    first = synthetic_day(60, business_date=business_date, run_tag="a", seed=7)
    again = synthetic_day(60, business_date=business_date, run_tag="a", seed=7)
    reseeded = synthetic_day(60, business_date=business_date, run_tag="a", seed=8)
    by_default = synthetic_day(60, business_date=business_date, run_tag="a")

    assert again == first
    assert by_default == first  # seed 7 is the default
    assert reseeded != first


def test_row_ids_carry_the_run_tag_and_account_ids_are_stable_across_tags(
    business_date,
):
    first = synthetic_day(40, business_date=business_date, run_tag="tag-a")
    second = synthetic_day(40, business_date=business_date, run_tag="tag-b")

    assert [r.event_id for r in first] == [f"row-tag-a-{n:06d}" for n in range(1, 41)]
    assert [r.event_id for r in second] == [f"row-tag-b-{n:06d}" for n in range(1, 41)]
    accounts = [f"acct-{n:06d}" for n in range(1, 41)]
    assert [r.account_id for r in first] == [r.account_id for r in second] == accounts
    # The same accounts in the same positions: only the row ids differ.
    assert [_without_id(r) for r in first] == [_without_id(r) for r in second]


def test_every_day_names_the_same_accounts_at_its_own_cut_off(business_date):
    today = synthetic_day(20, business_date=business_date, run_tag="day")
    other = synthetic_day(20, business_date=date(2026, 10, 23), run_tag="day")

    assert [r.account_id for r in today] == [r.account_id for r in other]
    assert [r.entity_id for r in today] == [r.entity_id for r in other]
    assert {r.as_of for r in today} == {
        datetime.combine(business_date, CUT_OFF, tzinfo=UTC)
    }
    assert {r.as_of for r in other} == {datetime(2026, 10, 23, 23, 59, 59, tzinfo=UTC)}


@pytest.mark.parametrize("seed", [DEFAULT_SEED, 8, 2026])
def test_the_first_five_rows_cover_every_band_then_one_exception(business_date, seed):
    scored = score_rows(
        synthetic_day(MIN_COUNT, business_date=business_date, run_tag="t", seed=seed)
    )

    recorded, exception = scored[:4], scored[4]
    assert [s.submission.mode for s in recorded] == ["record"] * 4
    assert [s.submission.bypass.rule_id for s in recorded] == [
        "current-account",
        "watch-list",
        "delinquent-hold",
        "charge-off",
    ]
    assert exception.is_exception
    assert exception.row.needs_review
    assert exception.submission.mode == "gate"


def test_every_unflagged_row_is_recorded_and_every_flagged_row_is_an_exception(
    business_date,
):
    scored = score_rows(synthetic_day(2_000, business_date=business_date, run_tag="t"))

    assert [s.is_exception for s in scored] == [s.row.needs_review for s in scored]
    assert {s.submission.mode for s in scored if not s.row.needs_review} == {"record"}
    assert sum(s.row.needs_review for s in scored) > 1


def test_a_day_below_the_minimum_count_is_refused(business_date):
    with pytest.raises(ValueError, match=f"at least {MIN_COUNT} rows, got 4"):
        synthetic_day(MIN_COUNT - 1, business_date=business_date, run_tag="t")


def test_a_default_day_plans_three_requests_of_at_most_10000_lines(business_date):
    rows = synthetic_day(business_date=business_date, run_tag="full")

    plan = plan_day(score_rows(rows))

    assert (DEFAULT_COUNT, DEFAULT_SEED, MAX_BATCH_LINES) == (25_000, 7, 10_000)
    assert len(rows) == len({r.event_id for r in rows}) == DEFAULT_COUNT
    assert [len(chunk) for chunk in plan.chunks] == [10_000, 10_000, 4_759]
    assert len(plan.exceptions) == 242
    assert plan.provoked == "row-full-000005"
    assert plan.chunks[0][4].event_id == plan.provoked
