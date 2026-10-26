"""The computation: pure arithmetic over one row, the same every time.

``compute_drift`` reads a row's target and actual amounts and gives the
drift between them: ``drift_amount`` (actual less target, to the cent),
``drift_pct`` (that as a percentage of the target, to four places; negative
for a shortfall, positive for an overage), and ``abs_drift_pct`` (its size,
whichever its sign). It is a function of the row's amounts alone: the same
row always gives the same result, and another row with the same amounts
gives it too. The feature set holds every value the computation read and
gave, and the narrative is the arithmetic with the row's own numbers.
"""

from __future__ import annotations

import pytest

from mock_daily_reasoner.compute import compute_drift
from mock_daily_reasoner.settlements import AccountSettlement


def test_the_same_row_always_gives_the_same_result(settlement):
    row = settlement(1, target=7_000.0, actual=6_000.0)

    first = compute_drift(row)

    assert all(compute_drift(row) == first for _ in range(3))
    assert compute_drift(AccountSettlement.from_payload(row.payload())) == first
    # A function of the amounts alone: another row id and account compute alike.
    same_amounts = settlement(2, target=7_000.0, actual=6_000.0, account="acct-z")
    assert compute_drift(same_amounts) == first
    assert (first.drift_amount, first.drift_pct, first.abs_drift_pct) == (
        -1_000.0,
        -14.2857,
        14.2857,
    )


@pytest.mark.parametrize(
    ("target", "actual", "drift_amount", "drift_pct"),
    [
        pytest.param(1_500.0, 1_500.0, 0.0, 0.0, id="settled-exactly"),
        pytest.param(2_500.0, 2_490.0, -10.0, -0.4, id="a-shortfall-is-negative"),
        pytest.param(5_000.0, 5_150.0, 150.0, 3.0, id="an-overage-is-positive"),
        pytest.param(3.0, 4.0, 1.0, 33.3333, id="the-percentage-to-four-places"),
        pytest.param(3.0, 2.0, -1.0, -33.3333, id="a-shortfall-to-four-places"),
        pytest.param(1_234.56, 1_240.0, 5.44, 0.4406, id="the-amount-to-the-cent"),
        pytest.param(10.0, 10.006, 0.01, 0.06, id="a-fraction-of-a-cent-rounds"),
        pytest.param(1_000.0, 0.0, -1_000.0, -100.0, id="nothing-settled"),
        pytest.param(1_000.0, 3_500.0, 2_500.0, 250.0, id="over-twice-the-target"),
    ],
)
def test_the_drift_is_the_actual_less_the_target_as_a_percentage_of_the_target(
    settlement, target, actual, drift_amount, drift_pct
):
    drift = compute_drift(settlement(1, target=target, actual=actual))

    assert (drift.target_amount, drift.actual_amount) == (target, actual)
    assert drift.drift_amount == drift_amount
    assert drift.drift_pct == drift_pct
    assert drift.abs_drift_pct == abs(drift_pct)


def test_the_size_of_the_drift_is_the_same_short_or_over(settlement):
    short = compute_drift(settlement(1, target=10_000.0, actual=9_200.0))
    over = compute_drift(settlement(2, target=10_000.0, actual=10_800.0))

    assert (short.drift_pct, over.drift_pct) == (-8.0, 8.0)
    assert short.abs_drift_pct == over.abs_drift_pct == 8.0
    assert short.abs_drift_pct == -short.drift_pct


def test_the_feature_set_holds_every_value_the_computation_read_and_gave(settlement):
    drift = compute_drift(settlement(1, target=2_500.0, actual=2_490.0))

    assert drift.feature_set == {
        "target_amount": 2_500.0,
        "actual_amount": 2_490.0,
        "drift_amount": -10.0,
        "drift_pct": -0.4,
        "abs_drift_pct": 0.4,
    }


def test_the_narrative_is_the_arithmetic_with_the_rows_numbers(settlement):
    drift = compute_drift(settlement(1, target=2_500.0, actual=2_490.0))

    assert drift.narrative == (
        "drift_pct = 100 * (actual_amount - target_amount) / target_amount "
        "= 100 * (2490.0 - 2500.0) / 2500.0 = -0.4"
    )
