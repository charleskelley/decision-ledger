"""Behavioral tests for Observation protocol conformance.

These tests verify DR-11's structural subtyping contract: domain types satisfy
framework protocols without inheritance. A StubEvent IS an Observation. If
these assertions break, the entire framework/domain boundary has been violated.

Why there is no test_observation.py:
The Observation protocol has no validators, computed fields, or business rules —
only four field declarations. Its entire contract is: "any type with these four
fields satisfies the protocol." That contract is fully exercised here — one
positive conformance test, and one negative test per required field (missing
each field breaks conformance). A separate test file would duplicate this
coverage without adding signal.
"""

from datetime import UTC, datetime

from core.observation import Observation
from core.routes import GateRoute


def test_stub_event_satisfies_observation_protocol(observation):
    assert isinstance(observation, Observation)


def test_observation_requires_event_id(observation):
    class MissingEventId:
        entity_id = observation.entity_id
        entity_type = "widget"
        timestamp = datetime.now(UTC)

    assert not isinstance(MissingEventId(), Observation)


def test_observation_requires_entity_id(observation):
    class MissingEntityId:
        event_id = "evt-001"
        entity_type = "widget"
        timestamp = datetime.now(UTC)

    assert not isinstance(MissingEntityId(), Observation)


def test_observation_requires_entity_type(observation):
    class MissingEntityType:
        event_id = "evt-001"
        entity_id = observation.entity_id
        timestamp = datetime.now(UTC)

    assert not isinstance(MissingEntityType(), Observation)


def test_observation_requires_timestamp(observation):
    class MissingTimestamp:
        event_id = "evt-001"
        entity_id = observation.entity_id
        entity_type = "widget"

    assert not isinstance(MissingTimestamp(), Observation)


def test_observation_satisfied_by_any_conforming_type(
    observation, gate_context, reasoner_context
):
    # Any object with all required Observation fields satisfies the protocol,
    # regardless of inheritance. This is structural subtyping (PEP 544).
    _gate_context = gate_context  # capture before class scope
    _reasoner_context = reasoner_context

    class MinimalObservation:
        event_id = "minimal-001"
        entity_id = observation.entity_id
        entity_type = "vendor"
        timestamp = datetime.now(UTC)
        route = GateRoute.ROUTE_TO_GATE
        reasoner_context = _reasoner_context
        fast_path_rationale = None
        gate_context = _gate_context

    assert isinstance(MinimalObservation(), Observation)
