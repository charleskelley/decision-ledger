"""Per-account sliding state: features read before a transfer joins, on event time.

``AccountWindows.observe`` returns a transfer's features from the account's
earlier transfers only, then adds it: the baseline is the mean of every
earlier amount, the window holds the earlier transfers within ten minutes
before its event time (whatever order they arrived in), and an account with
fewer than three earlier transfers has a thin history. Retention bounds the
window's memory, never the baseline or the count; ``reset`` forgets every
account.
"""

from __future__ import annotations

import itertools
from datetime import timedelta

import pytest

from mock_stream_reasoner.state import (
    REFERENCE_AMOUNT,
    WINDOW,
    AccountWindows,
    TransferFeatures,
)


def test_the_first_transfer_of_an_account_is_measured_against_the_reference_amount(
    transfer,
):
    windows = AccountWindows()

    features = windows.observe(transfer(1, "acct-a", 250.0))

    assert features == TransferFeatures(
        amount=250.0,
        baseline_amount=REFERENCE_AMOUNT,
        amount_ratio=250.0 / REFERENCE_AMOUNT,
        window_events=0,
        window_amount=0.0,
        prior_events=0,
        thin_history=True,
    )
    assert REFERENCE_AMOUNT == 100.0


def test_a_transfer_joins_its_account_only_after_its_own_features_are_read(transfer):
    windows = AccountWindows()

    first = windows.observe(transfer(1, "acct-a", 40.0, minutes=0))
    second = windows.observe(transfer(2, "acct-a", 80.0, minutes=1))

    assert (first.prior_events, first.window_events) == (0, 0)
    assert (second.prior_events, second.window_events) == (1, 1)
    assert (second.baseline_amount, second.window_amount) == (40.0, 40.0)


def test_a_transfer_is_measured_against_the_mean_of_its_accounts_earlier_amounts(
    transfer,
):
    windows = AccountWindows()
    windows.observe(transfer(1, "acct-a", 40.0, minutes=0))
    windows.observe(transfer(2, "acct-b", 5000.0, minutes=10))
    windows.observe(transfer(3, "acct-a", 80.0, minutes=30))

    features = windows.observe(transfer(4, "acct-a", 120.0, minutes=60))

    assert features.baseline_amount == 60.0
    assert features.amount_ratio == 2.0
    assert features.prior_events == 2
    assert len(windows) == 2


def test_the_window_holds_the_earlier_transfers_within_ten_minutes_of_event_time(
    transfer,
):
    windows = AccountWindows()
    windows.observe(transfer(1, "acct-a", 10.0, minutes=0))  # 11 minutes before
    windows.observe(transfer(2, "acct-a", 40.0, minutes=12))  # after it
    windows.observe(transfer(3, "acct-a", 20.0, minutes=8))  # out of order, inside
    windows.observe(transfer(4, "acct-a", 30.0, minutes=9))  # out of order, inside

    features = windows.observe(transfer(5, "acct-a", 100.0, minutes=11))

    # Event time throughout: the transfer at minute 12 arrived first but is
    # later, so neither the window nor the history counts it.
    assert features.window_events == 2
    assert features.window_amount == 50.0
    assert features.prior_events == 3
    assert features.baseline_amount == 20.0
    assert features.amount_ratio == 5.0
    assert timedelta(minutes=10) == WINDOW


def test_precursors_reordered_in_arrival_give_a_later_transfer_the_same_features(
    transfer,
):
    precursors = [
        transfer(1, "acct-a", 40.0, minutes=1),
        transfer(2, "acct-a", 60.0, minutes=3),
        transfer(3, "acct-a", 80.0, minutes=4),
    ]
    seen = set()

    for order in itertools.permutations(precursors):
        windows = AccountWindows()
        for event in order:
            windows.observe(event)
        seen.add(windows.observe(transfer(4, "acct-a", 300.0, minutes=6)))

    assert seen == {
        TransferFeatures(
            amount=300.0,
            baseline_amount=60.0,
            amount_ratio=5.0,
            window_events=3,
            window_amount=180.0,
            prior_events=3,
            thin_history=False,
        )
    }


def test_an_account_stops_being_thin_at_three_earlier_transfers(transfer):
    windows = AccountWindows()

    observed = [
        windows.observe(transfer(n, "acct-a", 100.0, minutes=20.0 * n))
        for n in range(5)
    ]

    assert [f.prior_events for f in observed] == [0, 1, 2, 3, 4]
    assert [f.thin_history for f in observed] == [True, True, True, False, False]


def test_retention_bounds_the_window_memory_but_the_history_keeps_every_transfer(
    transfer,
):
    windows = AccountWindows()  # a 10-minute window, kept for 20 minutes
    for n, amount in enumerate((100.0, 300.0, 200.0), 1):
        windows.observe(transfer(n, "acct-a", amount, minutes=n))
    windows.observe(transfer(4, "acct-a", 400.0, minutes=30))

    # 21 minutes behind the latest, past the exact zone (retention minus the
    # window), which the source's lateness bound never lets through: minutes
    # 1-3 are no longer kept whole, so its window misses them, but they still
    # count in its history, and the later transfer at minute 30 does not.
    straggler = windows.observe(transfer(5, "acct-a", 100.0, minutes=9))

    assert straggler.window_events == 0
    assert straggler.prior_events == 3
    assert straggler.baseline_amount == 200.0


def test_a_transfer_arriving_late_within_retention_still_sees_its_window(transfer):
    windows = AccountWindows()
    for n, amount in enumerate((100.0, 300.0, 200.0), 1):
        windows.observe(transfer(n, "acct-a", amount, minutes=n))
    windows.observe(transfer(4, "acct-a", 400.0, minutes=13))

    straggler = windows.observe(transfer(5, "acct-a", 100.0, minutes=9))

    assert straggler.window_events == 3
    assert straggler.window_amount == 600.0
    assert straggler.prior_events == 3  # not the later transfer at minute 13
    assert straggler.baseline_amount == 200.0
    assert straggler.thin_history is False


def test_a_late_transfer_is_scored_on_the_transfers_before_it_whatever_arrived_since(
    transfer,
):
    # A at minute 0, B at 1:40, C at 5:50; E at 3:20 arrives either before C
    # or after it (2.5 minutes behind, inside the lateness bound). Either way
    # its history is A and B: thin, so a gated E is held by Tier 3.
    a = transfer(1, "acct-a", 100.0, minutes=0)
    b = transfer(2, "acct-a", 100.0, minutes=100 / 60)
    c = transfer(3, "acct-a", 100.0, minutes=350 / 60)
    e = transfer(4, "acct-a", 450.0, minutes=200 / 60)

    seen = set()
    for order in ((a, b, e, c), (a, b, c, e)):
        windows = AccountWindows()
        features = {event.event_id: windows.observe(event) for event in order}
        seen.add(features[e.event_id])

    [scored] = seen
    assert (scored.prior_events, scored.window_events, scored.thin_history) == (
        2,
        2,
        True,
    )
    assert scored.baseline_amount == 100.0


def test_retention_is_never_shorter_than_the_window():
    assert AccountWindows().retention == 2 * WINDOW
    assert AccountWindows(retention=timedelta(minutes=1)).retention == WINDOW
    assert AccountWindows(retention=timedelta(hours=1)).retention == timedelta(hours=1)


def test_reset_forgets_every_account(transfer):
    windows = AccountWindows()
    windows.observe(transfer(1, "acct-a", 100.0))
    windows.observe(transfer(2, "acct-b", 100.0))
    windows.observe(transfer(3, "acct-a", 100.0, minutes=1))
    assert len(windows) == 2

    windows.reset()

    assert len(windows) == 0
    fresh = windows.observe(transfer(4, "acct-a", 900.0, minutes=2))
    assert (fresh.prior_events, fresh.window_events, fresh.thin_history) == (
        0,
        0,
        True,
    )
    assert fresh.baseline_amount == REFERENCE_AMOUNT
    assert len(windows) == 1


@pytest.mark.parametrize("window", [timedelta(0), timedelta(minutes=-10)])
def test_a_window_that_is_not_positive_is_refused(window):
    with pytest.raises(ValueError, match="window must be positive"):
        AccountWindows(window=window)


def test_the_feature_set_names_every_feature_with_its_json_type(transfer):
    windows = AccountWindows()

    empty_window = windows.observe(transfer(1, "acct-a", 100.0)).as_feature_set()
    full_window = windows.observe(
        transfer(2, "acct-a", 50.0, minutes=1)
    ).as_feature_set()

    for feature_set in (empty_window, full_window):
        assert {name: type(value) for name, value in feature_set.items()} == {
            "amount": float,
            "baseline_amount": float,
            "amount_ratio": float,
            "window_events": int,
            "window_amount": float,
            "prior_events": int,
            "thin_history": bool,
        }
    assert empty_window["window_amount"] == 0.0
    assert full_window["window_amount"] == 100.0
