"""Behavioral tests for generator.factory.EventFactory.

Tests verify that the factory produces valid LoginEvent objects and honors
each generation config dimension. Tests do not assert on internal factory
state — only on observable outputs (the LoginEvents themselves).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from generator.config import DeviceConsistency, GeoMobility, ScenarioConfig
from generator.factory import EventFactory
from generator.loader import load_scenario
from reasoner.account_takeover.events import AuthOutcome, LoginEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)


def _make_account_ids(n: int = 1) -> list[str]:
    return [f"acct_test_{i:04d}" for i in range(n)]


def _build_events(
    config: ScenarioConfig,
    count: int,
    account_id: str | None = None,
    seed: int = 42,
) -> list[LoginEvent]:
    ids = [account_id] if account_id else _make_account_ids(1)
    factory = EventFactory(config, ids, seed=seed)
    ts = _NOW
    return [factory.build_event(ids[0], ts) for _ in range(count)]


# ---------------------------------------------------------------------------
# Schema conformance
# ---------------------------------------------------------------------------


def test_generated_event_is_valid_login_event():
    config = load_scenario("baseline_normal")
    ids = _make_account_ids(1)
    factory = EventFactory(config, ids, seed=0)
    event = factory.build_event(ids[0], _NOW)
    # LoginEvent construction would raise ValidationError if schema violated
    assert isinstance(event, LoginEvent)


def test_scenario_tag_matches_scenario_id():
    for scenario_id in (
        "baseline_normal",
        "credential_stuffing_burst",
        "adversarial_probe",
    ):
        config = load_scenario(scenario_id)
        events = _build_events(config, count=3)
        for event in events:
            assert event.scenario_tag == scenario_id


def test_entity_id_is_deterministic_from_account_id():
    config = load_scenario("baseline_normal")
    ids = _make_account_ids(1)
    factory = EventFactory(config, ids, seed=0)
    e1 = factory.build_event(ids[0], _NOW)
    e2 = factory.build_event(ids[0], _NOW)
    assert e1.entity_id == e2.entity_id


def test_event_ids_are_unique():
    config = load_scenario("baseline_normal")
    events = _build_events(config, count=20)
    event_ids = [e.event_id for e in events]
    assert len(set(event_ids)) == 20


# ---------------------------------------------------------------------------
# Device fingerprint behaviour
# ---------------------------------------------------------------------------


def test_stable_device_produces_same_fingerprint_for_all_events():
    config = load_scenario("baseline_normal")
    assert config.generation.device.consistency == DeviceConsistency.STABLE
    events = _build_events(config, count=20)
    fingerprints = {e.device_fingerprint for e in events}
    assert len(fingerprints) == 1, "STABLE mode must use a single fingerprint"


def test_rotating_device_produces_multiple_distinct_fingerprints():
    config = load_scenario("credential_stuffing_burst")
    assert config.generation.device.consistency == DeviceConsistency.ROTATING
    events = _build_events(config, count=20)
    fingerprints = {e.device_fingerprint for e in events}
    assert len(fingerprints) > 1, (
        "ROTATING mode must cycle through multiple fingerprints"
    )


def test_partial_device_produces_structurally_similar_fingerprints():
    config = load_scenario("device_fingerprint_anomaly")
    assert config.generation.device.consistency == DeviceConsistency.PARTIAL
    events = _build_events(config, count=20)
    fingerprints = [e.device_fingerprint.split(":") for e in events]
    # All fingerprints must have 5 components
    assert all(len(fp) == 5 for fp in fingerprints)
    # First 3 components must be stable across all events (same account)
    first_stable = fingerprints[0][:3]
    for fp in fingerprints[1:]:
        assert fp[:3] == first_stable, (
            "PARTIAL mode: first 3 fingerprint components must be stable"
        )


# ---------------------------------------------------------------------------
# Geographic behaviour
# ---------------------------------------------------------------------------


def test_static_geo_always_returns_same_country():
    config = load_scenario("baseline_normal")
    assert config.generation.geo.mobility == GeoMobility.STATIC
    events = _build_events(config, count=20)
    countries = {e.geolocation.country for e in events}
    assert len(countries) == 1, "STATIC mobility must produce a single country"


def test_impossible_travel_produces_multiple_regions():
    config = load_scenario("geo_impossible")
    assert config.generation.geo.impossible_travel is True
    events = _build_events(config, count=20)
    countries = {e.geolocation.country for e in events}
    # Impossible travel requires intercontinental jumps — multiple countries expected
    assert len(countries) > 1, "Impossible travel events must span multiple countries"


def test_erratic_geo_spans_multiple_countries():
    config = load_scenario("credential_stuffing_burst")
    assert config.generation.geo.mobility == GeoMobility.ERRATIC
    events = _build_events(config, count=50)
    countries = {e.geolocation.country for e in events}
    assert len(countries) > 3, (
        "ERRATIC mobility should produce events across many countries"
    )


# ---------------------------------------------------------------------------
# Auth outcome distribution
# ---------------------------------------------------------------------------


def test_auth_outcome_distribution_respects_weights():
    """Outcome distribution should broadly match configured weights.

    Uses a large sample and asserts outcomes are within 3x of expected
    proportion — enough to catch wiring errors without flaking on variance.
    """
    config = load_scenario("credential_stuffing_burst")
    weights = config.generation.auth.outcome_weights
    events = _build_events(config, count=500)

    total = len(events)
    counts: dict[str, int] = {}
    for e in events:
        counts[e.outcome.value] = counts.get(e.outcome.value, 0) + 1

    weight_sum = sum(weights.values())
    for outcome_name, weight in weights.items():
        expected_fraction = weight / weight_sum
        actual_fraction = counts.get(outcome_name, 0) / total
        assert actual_fraction > expected_fraction / 3, (
            f"Outcome {outcome_name!r}: expected ~{expected_fraction:.2%}, "
            f"got {actual_fraction:.2%} — distribution appears broken"
        )


def test_high_success_rate_scenario_produces_mostly_successes():
    config = load_scenario("post_breach_ato")
    events = _build_events(config, count=200)
    successes = sum(1 for e in events if e.outcome == AuthOutcome.SUCCESS)
    assert successes / len(events) > 0.5, (
        "post_breach_ato expects ~80% SUCCESS outcomes"
    )


# ---------------------------------------------------------------------------
# Adversarial payload injection
# ---------------------------------------------------------------------------


def test_adversarial_user_agent_contains_injection_payload():
    config = load_scenario("adversarial_probe")
    assert config.generation.adversarial is not None
    assert config.generation.adversarial.user_agent_injection is True
    payload = config.generation.adversarial.user_agent_payload
    assert payload is not None

    events = _build_events(config, count=10)
    for event in events:
        assert payload in event.user_agent, (
            "Adversarial events must inject the exact payload into user_agent"
        )


# ---------------------------------------------------------------------------
# Novel entity
# ---------------------------------------------------------------------------


def test_novel_entity_scenario_produces_many_distinct_accounts():
    """novel_entity scenario uses 10 accounts; events should spread across them."""
    config = load_scenario("novel_entity")
    assert config.generation.novel_entity is True
    assert config.generation.user_count == 10

    ids = _make_account_ids(10)
    factory = EventFactory(config, ids, seed=0)
    events = [factory.build_event(ids[i % 10], _NOW) for i in range(15)]
    accounts_seen = {e.account_id for e in events}
    assert len(accounts_seen) > 1, (
        "Novel entity scenario should spread events across accounts"
    )


# ---------------------------------------------------------------------------
# Serialisation round-trip
# ---------------------------------------------------------------------------


def test_event_serialises_to_valid_json():
    config = load_scenario("baseline_normal")
    ids = _make_account_ids(1)
    factory = EventFactory(config, ids, seed=0)
    event = factory.build_event(ids[0], _NOW)
    raw = event.model_dump_json()
    parsed = json.loads(raw)
    assert parsed["account_id"] == ids[0]
    assert "entity_id" in parsed
    assert "scenario_tag" in parsed
