"""Behavioral tests for AtoFeatureVector and WindowSpec contracts.

Key contracts:
- entity_id on AtoFeatureVector matches entity_id on LoginEvent for the same
  account_id — the UUID5 namespace is shared, ensuring consistent framework
  identity across the event and feature layers.
- Novelty and consistency fields are bounded to [0.0, 1.0].
- Velocity fields are non-negative.
- AtoFeatureVector is immutable once constructed.
"""

import pytest
from pydantic import ValidationError

from reasoner.account_takeover.features import AtoFeatureVector, WindowSpec


def test_entity_id_matches_login_event_for_same_account_id(
    login_event, ato_feature_vector
):
    # Both LoginEvent and AtoFeatureVector derive entity_id from account_id
    # using the same UUID5 namespace. They must agree for the same account.
    assert ato_feature_vector.entity_id == login_event.entity_id


def test_entity_type_is_account_literal(ato_feature_vector):
    assert ato_feature_vector.entity_type == "account"


def test_novelty_fields_reject_values_above_one(ato_feature_vector):
    with pytest.raises(ValidationError):
        AtoFeatureVector(
            **{
                **ato_feature_vector.model_dump(),
                "ip_novelty": 1.1,
            }
        )


def test_novelty_fields_reject_negative_values(ato_feature_vector):
    with pytest.raises(ValidationError):
        AtoFeatureVector(
            **{
                **ato_feature_vector.model_dump(),
                "device_novelty": -0.01,
            }
        )


def test_consistency_score_rejects_values_above_one(ato_feature_vector):
    with pytest.raises(ValidationError):
        AtoFeatureVector(
            **{
                **ato_feature_vector.model_dump(),
                "device_consistency_score": 1.01,
            }
        )


def test_velocity_fields_reject_negative_values(ato_feature_vector):
    with pytest.raises(ValidationError):
        AtoFeatureVector(
            **{
                **ato_feature_vector.model_dump(),
                "velocity_1min": -1,
            }
        )


def test_travel_speed_may_be_none_when_no_prior_location(ato_feature_vector):
    assert ato_feature_vector.travel_speed_kmh is None


def test_impossible_travel_flag_can_be_set(ato_feature_vector):
    event = AtoFeatureVector(
        **{
            **ato_feature_vector.model_dump(),
            "impossible_travel": True,
            "travel_speed_kmh": 1800.0,
        }
    )
    assert event.impossible_travel
    assert event.travel_speed_kmh == 1800.0


def test_sparse_history_flag_is_settable(ato_feature_vector):
    sparse = AtoFeatureVector(
        **{**ato_feature_vector.model_dump(), "sparse_history": True}
    )
    assert sparse.sparse_history


def test_ato_feature_vector_is_immutable(ato_feature_vector):
    with pytest.raises(ValidationError):
        ato_feature_vector.ip_novelty = 0.5


def test_window_spec_rejects_zero_window_minutes():
    with pytest.raises(ValidationError):
        WindowSpec(window_minutes=0, available=True, event_count=5)


def test_window_spec_rejects_negative_event_count():
    with pytest.raises(ValidationError):
        WindowSpec(window_minutes=5, available=True, event_count=-1)


def test_window_spec_unavailable_with_zero_event_count():
    spec = WindowSpec(window_minutes=60, available=False, event_count=0)
    assert not spec.available
    assert spec.event_count == 0


def test_window_spec_is_immutable():
    spec = WindowSpec(window_minutes=5, available=True, event_count=3)
    with pytest.raises(ValidationError):
        spec.event_count = 10
