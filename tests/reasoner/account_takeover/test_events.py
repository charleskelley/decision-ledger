"""Behavioral tests for LoginEvent and authentication domain enums.

Key contracts:
- entity_id is deterministic: same account_id always produces the same UUID.
- entity_id is account-specific: different account_ids produce different UUIDs.
- scenario_tag is stripped before the pipeline sees the event.
- LoginEvent is immutable once constructed.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from reasoner.account_takeover.events import AuthMethod, AuthOutcome, LoginEvent


def test_entity_id_is_deterministic_for_same_account_id(geo_data):
    shared_kwargs = {
        "event_id": "evt-001",
        "timestamp": datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
        "account_id": "acc-12345",
        "session_id": "sess-001",
        "ip_address": "10.0.0.1",
        "geolocation": geo_data,
        "device_fingerprint": "fp-abc",
        "user_agent": "Mozilla/5.0",
        "auth_method": AuthMethod.PASSWORD,
        "outcome": AuthOutcome.SUCCESS,
    }
    event_a = LoginEvent(**shared_kwargs)
    event_b = LoginEvent(
        **{**shared_kwargs, "event_id": "evt-002", "session_id": "sess-002"}
    )

    assert event_a.entity_id == event_b.entity_id


def test_entity_id_differs_for_different_account_ids(geo_data):
    base = {
        "event_id": "evt-001",
        "timestamp": datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
        "session_id": "sess-001",
        "ip_address": "10.0.0.1",
        "geolocation": geo_data,
        "device_fingerprint": "fp-abc",
        "user_agent": "Mozilla/5.0",
        "auth_method": AuthMethod.PASSWORD,
        "outcome": AuthOutcome.SUCCESS,
    }
    event_a = LoginEvent(**{**base, "account_id": "acc-001"})
    event_b = LoginEvent(**{**base, "account_id": "acc-002"})

    assert event_a.entity_id != event_b.entity_id


def test_entity_id_is_a_uuid(login_event):
    from uuid import UUID

    assert isinstance(login_event.entity_id, UUID)


def test_entity_type_is_account_literal(login_event):
    assert login_event.entity_type == "account"


def test_scenario_tag_defaults_to_none(login_event):
    # scenario_tag must be absent from events that reach the pipeline.
    assert login_event.scenario_tag is None


def test_scenario_tag_can_be_set_for_generator_use(geo_data):
    event = LoginEvent(
        event_id="evt-gen-001",
        timestamp=datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
        account_id="acc-gen-001",
        session_id="sess-gen-001",
        ip_address="10.0.0.1",
        geolocation=geo_data,
        device_fingerprint="fp-gen",
        user_agent="Mozilla/5.0",
        auth_method=AuthMethod.MFA_TOTP,
        outcome=AuthOutcome.SUCCESS,
        scenario_tag="baseline_normal",
    )
    assert event.scenario_tag == "baseline_normal"


def test_login_event_is_immutable(login_event):
    with pytest.raises(ValidationError):
        login_event.account_id = "acc-tampered"


def test_auth_method_covers_expected_values():
    assert AuthMethod.PASSWORD == "PASSWORD"
    assert AuthMethod.MFA_TOTP == "MFA_TOTP"
    assert AuthMethod.MFA_PUSH == "MFA_PUSH"
    assert AuthMethod.SSO == "SSO"
    assert AuthMethod.PASSKEY == "PASSKEY"
    assert AuthMethod.MAGIC_LINK == "MAGIC_LINK"


def test_auth_outcome_covers_expected_values():
    assert AuthOutcome.SUCCESS == "SUCCESS"
    assert AuthOutcome.FAILURE == "FAILURE"
    assert AuthOutcome.TIMEOUT == "TIMEOUT"
    assert AuthOutcome.BLOCKED == "BLOCKED"


def test_geo_data_construction(geo_data):
    assert geo_data.country == "US"
    assert -90 <= geo_data.latitude <= 90
    assert -180 <= geo_data.longitude <= 180


def test_geo_data_is_immutable(geo_data):
    with pytest.raises(ValidationError):
        geo_data.country = "GB"
