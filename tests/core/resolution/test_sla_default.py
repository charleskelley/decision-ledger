"""Behavioral tests for SlaDefaultResolutionAttempt — SLA-expiry subclass."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from core.actions import DecisionAction
from core.resolution import (
    ResolutionAttempt,
    ResolutionStatus,
    SlaDefaultResolutionAttempt,
)


def _make(**overrides) -> SlaDefaultResolutionAttempt:
    base = {
        "decision_id": "dec-001",
        "attempt_id": "att-002",
        "sequence": 1,
        "started_at": datetime(2024, 1, 15, 11, 30, 0, tzinfo=UTC),
        "completed_at": datetime(2024, 1, 15, 11, 30, 0, tzinfo=UTC),
        "resolver_id": "system:sla_timer",
        "status": ResolutionStatus.COMPLETED,
        "resolution_action": DecisionAction.BLOCK,
        "note": "SLA_EXPIRY_DEFAULT — STANDARD tier defaults to BLOCK.",
        "account_tier": "STANDARD",
        "sla_window_seconds": 86400,
    }
    base.update(overrides)
    return SlaDefaultResolutionAttempt(**base)


def test_sla_default_construction():
    attempt = _make()
    assert attempt.resolver_kind == "SLA_DEFAULT"
    assert attempt.account_tier == "STANDARD"
    assert attempt.sla_window_seconds == 86400


def test_sla_default_rejects_negative_window():
    with pytest.raises(ValidationError):
        _make(sla_window_seconds=-1)


def test_sla_default_is_a_resolution_attempt():
    attempt = _make()
    assert isinstance(attempt, ResolutionAttempt)


def test_sla_default_is_frozen():
    attempt = _make()
    with pytest.raises(ValidationError):
        attempt.account_tier = "ENTERPRISE"
