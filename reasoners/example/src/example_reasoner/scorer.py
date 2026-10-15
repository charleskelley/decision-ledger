"""The example's scorer — a transparent heuristic, not a trained model.

``score_widget`` turns an inspection into the evidence a ``ReasonerContext``
carries: a ``widget_score`` in [0, 1] and the complete feature snapshot it
was computed from. It is a logistic of two measurements, so its bands are
easy to reason about::

    z = -3.0 + 4.0 * wobble + 0.5 * misalignment_mm
    widget_score = 1 / (1 + exp(-z))

A steady, aligned widget scores near 0 (the ``low-score`` bypass rule
records ALLOW); a wobbling, misaligned one near 1 (``high-score`` records
BLOCK); the middle asks the policy gate. ``thin_history`` (fewer than
``THIN_HISTORY_INSPECTIONS`` prior inspections) is the novel-entity signal
the registration names for Tier 3.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from core.observation import Contribution
from example_reasoner.events import WidgetEvent  # noqa: TC001 (runtime use)

#: Version recorded as the ``ReasonerContext.model_version``.
SCORER_VERSION = "widget-heuristic-v1"

#: Fewer prior inspections than this is a thin history (Tier 3).
THIN_HISTORY_INSPECTIONS = 10

_INTERCEPT = -3.0
_WOBBLE_WEIGHT = 4.0
_MISALIGNMENT_WEIGHT = 0.5


@dataclass(frozen=True)
class WidgetScore:
    """The scorer's output for one inspection.

    Args:
        widget_score: The score in [0, 1].
        feature_set: Every feature the score was computed from.
        contributions: Each measurement's term in the logit.
    """

    widget_score: float
    feature_set: dict[str, float | int | str | bool]
    contributions: list[Contribution]


def score_widget(event: WidgetEvent) -> WidgetScore:
    """Score one inspection. Pure and deterministic.

    Args:
        event: The inspection.

    Returns:
        The score, its feature snapshot, and the per-feature contributions.
    """
    wobble_term = _WOBBLE_WEIGHT * event.wobble
    misalignment_term = _MISALIGNMENT_WEIGHT * event.misalignment_mm
    z = _INTERCEPT + wobble_term + misalignment_term
    score = round(1.0 / (1.0 + math.exp(-z)), 6)
    return WidgetScore(
        widget_score=score,
        feature_set={
            "wobble": event.wobble,
            "misalignment_mm": event.misalignment_mm,
            "prior_inspections": event.prior_inspections,
            "thin_history": event.prior_inspections < THIN_HISTORY_INSPECTIONS,
            "region": event.region,
        },
        contributions=[
            Contribution(
                feature_name="wobble",
                feature_value=event.wobble,
                method="logit_term",
                value=round(wobble_term, 6),
            ),
            Contribution(
                feature_name="misalignment_mm",
                feature_value=event.misalignment_mm,
                method="logit_term",
                value=round(misalignment_term, 6),
            ),
        ],
    )
