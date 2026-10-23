"""The scorer: a transparent logistic whose bands are the registration's.

A transfer that fits its account's history scores below the steady band's
threshold (recorded ALLOW), a burst far above the account's pace above the
surge threshold (recorded BLOCK), and a large transfer, or a fresh account's
large first one, between them (the policy gate decides). The score is a pure
function of the features, and its contributions are the terms of the logit.
"""

from __future__ import annotations

import math

from mock_stream_reasoner.registry import STEADY_THRESHOLD, SURGE_THRESHOLD
from mock_stream_reasoner.scorer import score_transfer
from mock_stream_reasoner.state import AccountWindows

#: Three earlier transfers of 100, twenty minutes apart: an established account.
ESTABLISHED = [(0.0, 100.0), (20.0, 100.0), (40.0, 100.0)]


def _score_after(transfer, history, amount, *, minutes):
    """Score a transfer of ``amount`` after ``history`` ([(minutes, amount)])."""
    windows = AccountWindows()
    for n, (at, earlier) in enumerate(history, 1):
        windows.observe(transfer(n, "acct-score", earlier, minutes=at))
    last = transfer(len(history) + 1, "acct-score", amount, minutes=minutes)
    return score_transfer(windows.observe(last))


def test_a_steady_transfer_of_an_established_account_scores_in_the_allow_band(
    transfer,
):
    score = _score_after(transfer, ESTABLISHED, 100.0, minutes=60.0)

    assert score.features.thin_history is False
    assert 0.0 <= score.activity_score < STEADY_THRESHOLD


def test_a_small_first_transfer_of_a_fresh_account_scores_in_the_allow_band(
    transfer,
):
    score = _score_after(transfer, [], 50.0, minutes=0.0)

    assert score.features.thin_history is True
    assert 0.0 <= score.activity_score < STEADY_THRESHOLD


def test_a_large_first_transfer_of_a_fresh_account_lands_between_the_bands(
    transfer,
):
    score = _score_after(transfer, [], 500.0, minutes=0.0)

    assert score.features.thin_history is True
    assert STEADY_THRESHOLD < score.activity_score < SURGE_THRESHOLD


def test_a_transfer_at_six_times_an_established_baseline_lands_between_the_bands(
    transfer,
):
    score = _score_after(transfer, ESTABLISHED, 600.0, minutes=60.0)

    assert (score.features.amount_ratio, score.features.window_events) == (6.0, 0)
    assert STEADY_THRESHOLD < score.activity_score < SURGE_THRESHOLD


def test_a_burst_of_six_transfers_in_the_window_scores_in_the_block_band(transfer):
    burst = [(float(minute), 100.0) for minute in range(6)]

    score = _score_after(transfer, burst, 100.0, minutes=6.0)

    assert score.features.window_events == 6
    assert SURGE_THRESHOLD < score.activity_score <= 1.0


def test_the_same_stream_scores_the_same(transfer):
    history = [(0.0, 80.0), (3.0, 120.0)]

    first = _score_after(transfer, history, 450.0, minutes=7.0)
    second = _score_after(transfer, history, 450.0, minutes=7.0)

    assert first == second
    assert first.activity_score == round(first.activity_score, 6)


def test_the_contributions_are_the_terms_of_the_logit(transfer):
    score = _score_after(transfer, [(0.0, 100.0), (4.0, 100.0)], 300.0, minutes=8.0)

    assert [c.feature_name for c in score.contributions] == [
        "window_events",
        "amount_ratio",
        "thin_history",
    ]
    assert [c.feature_value for c in score.contributions] == [
        2.0,
        score.features.amount_ratio,
        1.0,
    ]
    assert all(type(c.feature_value) is float for c in score.contributions)
    assert {c.method for c in score.contributions} == {"logit_term"}
    logit = -4.0 + sum(c.value for c in score.contributions)
    assert math.isclose(
        score.activity_score, 1.0 / (1.0 + math.exp(-logit)), abs_tol=1e-5
    )


def test_the_score_carries_every_feature_it_read(transfer):
    score = _score_after(transfer, ESTABLISHED, 250.0, minutes=45.0)

    assert score.feature_set == score.features.as_feature_set()
    assert set(score.feature_set) == {
        "amount",
        "baseline_amount",
        "amount_ratio",
        "window_events",
        "window_amount",
        "prior_events",
        "thin_history",
    }
