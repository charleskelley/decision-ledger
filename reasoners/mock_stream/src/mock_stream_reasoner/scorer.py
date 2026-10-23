"""The stream mock's scorer — a transparent heuristic, not a trained model.

``score_transfer`` turns a transfer's feature snapshot (``state``) into the
evidence a ``ReasonerContext`` carries: an ``activity_score`` in [0, 1] and
every feature it read. It is a logistic, so its bands are easy to reason
about::

    z = -4.0 + 0.9 * window_events + 1.6 * ln(1 + amount_ratio)
             + 1.2 * thin_history
    activity_score = 1 / (1 + exp(-z))

A transfer that fits its account's history scores near 0 (``steady-activity``
records ALLOW); a burst of transfers far above the account's baseline near 1
(``activity-surge`` records BLOCK); a large transfer, or the first large
transfer of a fresh account, lands between the bands and asks the policy
gate. ``thin_history`` both raises the score and is the novel-entity signal
the registration names for Tier 3.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.observation import Contribution

if TYPE_CHECKING:
    from mock_stream_reasoner.state import TransferFeatures

#: Version recorded as the ``ReasonerContext.model_version``.
SCORER_VERSION = "transfer-heuristic-v1"

_INTERCEPT = -4.0
_VELOCITY_WEIGHT = 0.9
_AMOUNT_WEIGHT = 1.6
_THIN_HISTORY_WEIGHT = 1.2


@dataclass(frozen=True)
class ActivityScore:
    """The scorer's output for one transfer.

    Args:
        activity_score: The score in [0, 1].
        features: The feature snapshot it was computed from.
        contributions: Each term of the logit.
    """

    activity_score: float
    features: TransferFeatures
    contributions: list[Contribution]

    @property
    def feature_set(self) -> dict[str, float | int | str | bool]:
        """Every feature the score read: the ``ReasonerContext.feature_set``."""
        return self.features.as_feature_set()


def score_transfer(features: TransferFeatures) -> ActivityScore:
    """Score one transfer from its features. Pure and deterministic.

    Args:
        features: The transfer's snapshot, read from the account's state.

    Returns:
        The score, its features, and the per-feature contributions.
    """
    velocity_term = _VELOCITY_WEIGHT * features.window_events
    amount_term = _AMOUNT_WEIGHT * math.log1p(features.amount_ratio)
    thin_term = _THIN_HISTORY_WEIGHT if features.thin_history else 0.0
    z = _INTERCEPT + velocity_term + amount_term + thin_term
    return ActivityScore(
        activity_score=round(1.0 / (1.0 + math.exp(-z)), 6),
        features=features,
        contributions=[
            Contribution(
                feature_name="window_events",
                feature_value=float(features.window_events),
                method="logit_term",
                value=round(velocity_term, 6),
            ),
            Contribution(
                feature_name="amount_ratio",
                feature_value=features.amount_ratio,
                method="logit_term",
                value=round(amount_term, 6),
            ),
            Contribution(
                feature_name="thin_history",
                feature_value=float(features.thin_history),
                method="logit_term",
                value=round(thin_term, 6),
            ),
        ],
    )
