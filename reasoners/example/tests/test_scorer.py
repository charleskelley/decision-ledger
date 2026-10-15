"""The example's scorer is a documented, deterministic heuristic.

Its bands decide the mode: low → recorded ALLOW, high → recorded BLOCK,
between → the policy gate. ``thin_history`` is the Tier 3 signal.
"""

from __future__ import annotations

import pytest

from example_reasoner.__main__ import BANDS, sample_event
from example_reasoner.registry import HIGH_SCORE_THRESHOLD, LOW_SCORE_THRESHOLD
from example_reasoner.scorer import SCORER_VERSION, score_widget


@pytest.mark.parametrize(
    ("band", "check"),
    [
        ("low", lambda s: s < LOW_SCORE_THRESHOLD),
        ("mid", lambda s: LOW_SCORE_THRESHOLD <= s <= HIGH_SCORE_THRESHOLD),
        ("high", lambda s: s > HIGH_SCORE_THRESHOLD),
    ],
)
def test_each_band_scores_where_its_name_says(band, check):
    assert check(score_widget(sample_event(band)).widget_score)


def test_the_score_is_the_documented_logistic():
    score = score_widget(sample_event("mid")).widget_score
    assert score == pytest.approx(0.5)  # -3 + 4(0.5) + 0.5(2.0) = 0


def test_scoring_is_deterministic_and_reports_every_feature():
    event = sample_event("low", prior_inspections=3)
    first, second = score_widget(event), score_widget(event)
    assert first == second
    assert first.feature_set == {
        "wobble": BANDS["low"][0],
        "misalignment_mm": BANDS["low"][1],
        "prior_inspections": 3,
        "thin_history": True,
        "region": "REGION_A",
    }
    assert [c.feature_name for c in first.contributions] == [
        "wobble",
        "misalignment_mm",
    ]
    assert SCORER_VERSION == "widget-heuristic-v1"


def test_ten_prior_inspections_is_not_a_thin_history():
    assert (
        score_widget(sample_event("low", prior_inspections=10)).feature_set[
            "thin_history"
        ]
        is False
    )
