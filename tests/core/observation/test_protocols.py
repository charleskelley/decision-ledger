"""Behavioral tests for Observation protocol conformance.

These tests verify DR-11's structural subtyping contract: domain types satisfy
framework protocols without inheritance. A LoginEvent IS an Observation. If
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


def test_login_event_satisfies_observation_protocol(login_event):
    assert isinstance(login_event, Observation)


def test_observation_requires_event_id(login_event):
    class MissingEventId:
        entity_id = login_event.entity_id
        entity_type = "account"
        timestamp = datetime.now(UTC)

    assert not isinstance(MissingEventId(), Observation)


def test_observation_requires_entity_id(login_event):
    class MissingEntityId:
        event_id = "evt-001"
        entity_type = "account"
        timestamp = datetime.now(UTC)

    assert not isinstance(MissingEntityId(), Observation)


def test_observation_requires_entity_type(login_event):
    class MissingEntityType:
        event_id = "evt-001"
        entity_id = login_event.entity_id
        timestamp = datetime.now(UTC)

    assert not isinstance(MissingEntityType(), Observation)


def test_observation_requires_timestamp(login_event):
    class MissingTimestamp:
        event_id = "evt-001"
        entity_id = login_event.entity_id
        entity_type = "account"

    assert not isinstance(MissingTimestamp(), Observation)


def test_observation_satisfied_by_any_conforming_type(
    login_event, gate_context, reasoner_context
):
    # Any object with all required Observation fields satisfies the protocol,
    # regardless of inheritance. This is structural subtyping (PEP 544).
    _gate_context = gate_context  # capture before class scope
    _reasoner_context = reasoner_context

    class MinimalObservation:
        event_id = "minimal-001"
        entity_id = login_event.entity_id
        entity_type = "vendor"
        timestamp = datetime.now(UTC)
        route = GateRoute.ROUTE_TO_GATE
        reasoner_context = _reasoner_context
        fast_path_rationale = None
        gate_context = _gate_context

    assert isinstance(MinimalObservation(), Observation)
