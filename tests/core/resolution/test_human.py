"""Behavioral tests for HumanResolutionAttempt — analyst-review subclass."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from core.actions import DecisionAction
from core.resolution import HumanResolutionAttempt, ResolutionAttempt, ResolutionStatus


def _make(**overrides) -> HumanResolutionAttempt:
    base = {
        "decision_id": "dec-001",
        "attempt_id": "att-001",
        "sequence": 0,
        "started_at": datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
        "completed_at": datetime(2024, 1, 15, 10, 35, 0, tzinfo=UTC),
        "resolver_id": "user:reviewer-42",
        "status": ResolutionStatus.COMPLETED,
        "resolution_action": DecisionAction.ALLOW,
        "note": "Confirmed legitimate via support call.",
    }
    base.update(overrides)
    return HumanResolutionAttempt(**base)


def test_human_resolution_attempt_construction():
    attempt = _make(
        reviewer_role="senior-reviewer",
        reference_ticket_id="JIRA-1234",
    )
    assert attempt.resolver_kind == "HUMAN"
    assert attempt.reviewer_role == "senior-reviewer"
    assert attempt.reference_ticket_id == "JIRA-1234"


def test_human_resolution_attempt_optional_fields_default_none():
    attempt = _make()
    assert attempt.reviewer_role is None
    assert attempt.reference_ticket_id is None


def test_human_resolution_attempt_is_a_resolution_attempt():
    attempt = _make()
    assert isinstance(attempt, ResolutionAttempt)


def test_human_resolution_attempt_is_frozen():
    attempt = _make()
    with pytest.raises(ValidationError):
        attempt.note = "tampered"
