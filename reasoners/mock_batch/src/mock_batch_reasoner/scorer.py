"""The batch mock's scorer — a transparent heuristic, not a trained model.

``score_row`` turns one account's end-of-day position into the evidence a
``ReasonerContext`` carries: a ``delinquency_score`` in [0, 1] and every
feature it read. It is a logistic, so its bands are easy to reason about::

    z = -3.2 + 2.2 * min(utilization, 1.5) + 0.035 * days_past_due
             + 0.8 * returned_payments
    delinquency_score = 1 / (1 + exp(-z))

A current account scores low (``current-account`` records ALLOW), one
falling behind lands in the middle bands (``watch-list`` records CHALLENGE,
``delinquent-hold`` records HOLD), and one far past due with returned
payments near 1 (``charge-off`` records BLOCK). The score never reads
``needs_review``; the bypass rules do, and none covers a flagged row.

The score is a function of the row alone: a batch decides each row the same
whatever else the day holds, so the order of the rows never matters.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.observation import Contribution

if TYPE_CHECKING:
    from mock_batch_reasoner.rows import AccountDay

#: Version recorded as the ``ReasonerContext.model_version``.
SCORER_VERSION = "delinquency-heuristic-v1"

_INTERCEPT = -3.2
_UTILIZATION_WEIGHT = 2.2
#: Utilization beyond this adds nothing more to the score.
_UTILIZATION_CAP = 1.5
_PAST_DUE_WEIGHT = 0.035
_RETURNED_WEIGHT = 0.8


@dataclass(frozen=True)
class RowFeatures:
    """The feature snapshot of one row.

    Attributes:
        balance: The amount owed.
        credit_limit: The credit limit.
        utilization: ``balance / credit_limit``.
        days_past_due: Days since the oldest unpaid minimum payment was due.
        returned_payments: Payments returned unpaid in the last 30 days.
        needs_review: The row is flagged for review upstream.
    """

    balance: float
    credit_limit: float
    utilization: float
    days_past_due: int
    returned_payments: int
    needs_review: bool

    def as_feature_set(self) -> dict[str, float | int | str | bool]:
        """Every feature by name: the ``ReasonerContext.feature_set``."""
        return {
            "balance": self.balance,
            "credit_limit": self.credit_limit,
            "utilization": self.utilization,
            "days_past_due": self.days_past_due,
            "returned_payments": self.returned_payments,
            "needs_review": self.needs_review,
        }


@dataclass(frozen=True)
class DelinquencyScore:
    """The scorer's output for one row.

    Args:
        delinquency_score: The score in [0, 1].
        features: The feature snapshot it was computed from.
        contributions: Each term of the logit.
    """

    delinquency_score: float
    features: RowFeatures
    contributions: list[Contribution]

    @property
    def feature_set(self) -> dict[str, float | int | str | bool]:
        """Every feature the score and the bypass rules read."""
        return self.features.as_feature_set()


def row_features(row: AccountDay) -> RowFeatures:
    """Read one row's features.

    Args:
        row: The account's end-of-day position.

    Returns:
        Its feature snapshot.
    """
    return RowFeatures(
        balance=row.balance,
        credit_limit=row.credit_limit,
        utilization=round(row.balance / row.credit_limit, 4),
        days_past_due=row.days_past_due,
        returned_payments=row.returned_payments,
        needs_review=row.needs_review,
    )


def score_row(row: AccountDay) -> DelinquencyScore:
    """Score one row. Pure and deterministic.

    Args:
        row: The account's end-of-day position.

    Returns:
        The score, its features, and the per-feature contributions.
    """
    features = row_features(row)
    utilization_term = _UTILIZATION_WEIGHT * min(features.utilization, _UTILIZATION_CAP)
    past_due_term = _PAST_DUE_WEIGHT * features.days_past_due
    returned_term = _RETURNED_WEIGHT * features.returned_payments
    z = _INTERCEPT + utilization_term + past_due_term + returned_term
    return DelinquencyScore(
        delinquency_score=round(1.0 / (1.0 + math.exp(-z)), 6),
        features=features,
        contributions=[
            Contribution(
                feature_name="utilization",
                feature_value=features.utilization,
                method="logit_term",
                value=round(utilization_term, 6),
            ),
            Contribution(
                feature_name="days_past_due",
                feature_value=float(features.days_past_due),
                method="logit_term",
                value=round(past_due_term, 6),
            ),
            Contribution(
                feature_name="returned_payments",
                feature_value=float(features.returned_payments),
                method="logit_term",
                value=round(returned_term, 6),
            ),
        ],
    )
