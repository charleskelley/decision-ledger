"""The synthetic day: a deterministic reconciliation extract that covers every band.

``synthetic_day`` gives the same rows for the same arguments and other rows
for another seed. Row ids carry the run tag, so two runs never share an
``event_id``; account ids do not, so every day names the same accounts, at
its own cut-off. Its first ``MIN_COUNT`` rows are one of each profile: an
account within tolerance (recorded), then a minor, a major, and a severe
exception. Every row's drift sits well inside one band, short or over, and
about 95% of a default day of 1,000 accounts settles within tolerance. A
day below ``MIN_COUNT`` rows is refused.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime

import pytest

from mock_daily_reasoner.daily import compute_day
from mock_daily_reasoner.synthetic import (
    CUT_OFF,
    DEFAULT_COUNT,
    DEFAULT_SEED,
    MIN_COUNT,
    synthetic_day,
)

#: The drift each profile draws, in percent (before its sign), and the
#: band it falls in.
PROFILES = {
    "in_tolerance": (0.0, 0.9),
    "minor": (1.5, 4.5),
    "major": (6.0, 18.0),
    "severe": (25.0, 60.0),
}
#: How far cent rounding can move a row's drift (half a cent on the
#: smallest target, 1,000), in percent.
ROUNDING = 0.001


def _without_id(row):
    """A row's payload without its id: the account's settlement."""
    return {k: v for k, v in row.payload().items() if k != "event_id"}


def _profile(abs_drift_pct):
    """The profile whose range holds a drift, within cent rounding."""
    [profile] = [
        name
        for name, (low, high) in PROFILES.items()
        if low - ROUNDING <= abs_drift_pct <= high + ROUNDING
    ]
    return profile


def test_the_same_arguments_give_the_same_day_and_another_seed_another(
    business_date,
):
    first = synthetic_day(
        60, business_date=business_date, run_tag="a", seed=DEFAULT_SEED
    )
    again = synthetic_day(
        60, business_date=business_date, run_tag="a", seed=DEFAULT_SEED
    )
    reseeded = synthetic_day(60, business_date=business_date, run_tag="a", seed=8)
    by_default = synthetic_day(60, business_date=business_date, run_tag="a")

    assert again == first
    assert by_default == first
    assert reseeded != first


def test_row_ids_carry_the_run_tag_and_account_ids_are_stable_across_tags(
    business_date,
):
    first = synthetic_day(40, business_date=business_date, run_tag="tag-a")
    second = synthetic_day(40, business_date=business_date, run_tag="tag-b")

    assert [r.event_id for r in first] == [f"set-tag-a-{n:06d}" for n in range(1, 41)]
    assert [r.event_id for r in second] == [f"set-tag-b-{n:06d}" for n in range(1, 41)]
    accounts = [f"acct-{n:06d}" for n in range(1, 41)]
    assert [r.account_id for r in first] == [r.account_id for r in second] == accounts
    # The same accounts with the same amounts: only the row ids differ.
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
def test_the_first_rows_are_one_of_each_profile(business_date, seed):
    computed = compute_day(
        synthetic_day(MIN_COUNT, business_date=business_date, run_tag="t", seed=seed)
    )

    assert [_profile(c.drift.abs_drift_pct) for c in computed] == list(PROFILES)
    assert [c.submission.mode for c in computed] == ["record", "gate", "gate", "gate"]


def test_every_rows_drift_sits_well_inside_one_band_short_or_over(business_date):
    computed = compute_day(synthetic_day(business_date=business_date, run_tag="t"))

    profiles = Counter(_profile(c.drift.abs_drift_pct) for c in computed)
    assert set(profiles) == set(PROFILES)
    for c in computed:
        assert c.is_exception == (_profile(c.drift.abs_drift_pct) != "in_tolerance")
    # The drift is a shortfall or an overage at random, in every band.
    for name in PROFILES:
        signs = {
            c.drift.drift_pct > 0
            for c in computed
            if _profile(c.drift.abs_drift_pct) == name and c.drift.drift_pct != 0
        }
        assert signs == {True, False}, name


def test_about_95_percent_of_a_default_day_settles_within_tolerance(business_date):
    rows = synthetic_day(business_date=business_date, run_tag="full")

    computed = compute_day(rows)

    assert (DEFAULT_COUNT, DEFAULT_SEED, MIN_COUNT) == (1_000, 11, 4)
    assert len(rows) == len({r.event_id for r in rows}) == DEFAULT_COUNT
    recorded = sum(not c.is_exception for c in computed)
    assert 900 <= recorded <= 990
    # Every severity is present among the exceptions.
    severities = {_profile(c.drift.abs_drift_pct) for c in computed if c.is_exception}
    assert severities == {"minor", "major", "severe"}


def test_a_day_below_the_minimum_count_is_refused(business_date):
    with pytest.raises(ValueError, match=f"at least {MIN_COUNT} rows, got 3"):
        synthetic_day(MIN_COUNT - 1, business_date=business_date, run_tag="t")
