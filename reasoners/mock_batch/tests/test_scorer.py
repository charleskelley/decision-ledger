"""The scorer: a transparent logistic of one row, read only from the row.

``score_row`` reads a row's six features and scores it in [0, 1]. The score
rises with utilization (which adds nothing past 1.5 times the limit), with
days past due, and with returned payments; it never reads the review flag
(the bypass rules do), and it is a pure function of the row, so a day's
other rows never change it. Its contributions are the terms of the logit:
the intercept plus the terms is the logit of the score.
"""

from __future__ import annotations

import itertools
import math

import pytest

from mock_batch_reasoner.rows import AccountDay
from mock_batch_reasoner.scorer import score_row

#: The logit's intercept (``mock_batch_reasoner/scorer.py``).
INTERCEPT = -3.2


@pytest.mark.parametrize(
    "position",
    [
        pytest.param({"balance": 0.0}, id="nothing-owed"),
        pytest.param({"balance": 250.0}, id="current"),
        pytest.param(
            {
                "balance": 1_000_000.0,
                "credit_limit": 500.0,
                "days_past_due": 3650,
                "returned_payments": 100,
            },
            id="far-gone",
        ),
    ],
)
def test_the_score_is_in_the_unit_interval(row, position):
    score = score_row(row(1, **position))

    assert 0.0 <= score.delinquency_score <= 1.0


@pytest.mark.parametrize(
    ("feature", "values"),
    [
        pytest.param(
            "balance", [0.0, 300.0, 700.0, 1_100.0, 1_400.0], id="utilization"
        ),
        pytest.param("days_past_due", [0, 30, 60, 90, 120], id="days-past-due"),
        pytest.param("returned_payments", [0, 1, 2, 3], id="returned-payments"),
    ],
)
def test_the_score_rises_with_utilization_days_past_due_and_returned_payments(
    row, feature, values
):
    scores = [
        score_row(row(1, **{feature: value})).delinquency_score for value in values
    ]

    assert all(lower < higher for lower, higher in itertools.pairwise(scores))


def test_utilization_beyond_the_cap_adds_nothing(row):
    below = score_row(row(1, balance=1_400.0))
    at_cap = score_row(row(2, balance=1_500.0))
    beyond = score_row(row(3, balance=4_000.0))

    # The feature is the utilization read; only its term is capped.
    assert (at_cap.features.utilization, beyond.features.utilization) == (1.5, 4.0)
    assert beyond.delinquency_score == at_cap.delinquency_score
    assert beyond.contributions[0].value == at_cap.contributions[0].value
    assert below.delinquency_score < at_cap.delinquency_score


def test_the_same_row_scores_the_same(row):
    position = row(1, balance=640.0, days_past_due=30, returned_payments=1)

    first = score_row(position)

    assert score_row(position) == first
    assert score_row(AccountDay.from_payload(position.payload())) == first
    assert first.delinquency_score == round(first.delinquency_score, 6)
    # A function of the position alone: another row id and account score alike.
    same_position = row(
        2, balance=640.0, days_past_due=30, returned_payments=1, account="acct-z"
    )
    assert score_row(same_position) == first


def test_the_score_never_reads_the_review_flag(row):
    unflagged = score_row(row(1, balance=950.0, days_past_due=60))
    flagged = score_row(row(1, balance=950.0, days_past_due=60, needs_review=True))

    assert flagged.delinquency_score == unflagged.delinquency_score
    assert flagged.contributions == unflagged.contributions
    assert (flagged.features.needs_review, unflagged.features.needs_review) == (
        True,
        False,
    )
    differing = {
        name
        for name, value in flagged.feature_set.items()
        if unflagged.feature_set[name] != value
    }
    assert differing == {"needs_review"}


@pytest.mark.parametrize(
    "position",
    [
        pytest.param({"balance": 250.0}, id="current"),
        pytest.param({"balance": 850.0, "days_past_due": 30}, id="watch"),
        pytest.param({"balance": 950.0, "days_past_due": 60}, id="delinquent"),
        pytest.param(
            {"balance": 1_200.0, "days_past_due": 90, "returned_payments": 2},
            id="charge-off",
        ),
    ],
)
def test_the_contributions_are_the_terms_of_the_logit(row, position):
    score = score_row(row(1, **position))
    features = score.features

    assert [c.feature_name for c in score.contributions] == [
        "utilization",
        "days_past_due",
        "returned_payments",
    ]
    assert [c.feature_value for c in score.contributions] == [
        features.utilization,
        float(features.days_past_due),
        float(features.returned_payments),
    ]
    assert all(type(c.feature_value) is float for c in score.contributions)
    assert {c.method for c in score.contributions} == {"logit_term"}
    p = score.delinquency_score
    logit = INTERCEPT + sum(c.value for c in score.contributions)
    # Within rounding: the score and each term are kept to six decimals.
    assert math.isclose(logit, math.log(p / (1.0 - p)), abs_tol=1e-4)


def test_the_feature_set_holds_exactly_the_six_features_of_the_row(row):
    position = row(
        1,
        balance=640.0,
        credit_limit=2_500.0,
        days_past_due=30,
        returned_payments=1,
        needs_review=True,
    )

    score = score_row(position)

    assert score.feature_set == {
        "balance": 640.0,
        "credit_limit": 2_500.0,
        "utilization": 0.256,
        "days_past_due": 30,
        "returned_payments": 1,
        "needs_review": True,
    }
    assert score.feature_set == score.features.as_feature_set()
