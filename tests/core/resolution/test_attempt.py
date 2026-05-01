"""Behavioral tests for the universal ResolutionAttempt base.

The framework's universal ``ResolutionAttempt`` carries only the fields
every resolver kind has in common. Per-resolver-kind subclasses
(``HumanResolutionAttempt`` etc.) extend it with typed kind-specific
fields — those tests live in sibling files.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from core.actions import DecisionAction
from core.resolution import (
    ResolutionAttempt,
    ResolutionStatus,
    ResolverKind,
)


def test_resolver_kind_enum_includes_mvp_and_wireframed_values():
    assert ResolverKind.HUMAN.value == "HUMAN"
    assert ResolverKind.SLA_DEFAULT.value == "SLA_DEFAULT"
    for member in (
        "STEP_UP_AUTH",
        "AUTOMATED_OUTREACH",
        "SECOND_OPINION",
        "EXTERNAL_TICKET",
        "SELF_SERVICE",
        "OVERRIDE",
        "ESCALATION",
    ):
        assert member in ResolverKind.__members__


def test_resolution_status_lifecycle_values():
    assert {m.value for m in ResolutionStatus} == {
        "PENDING",
        "COMPLETED",
        "ESCALATED",
        "EXPIRED",
    }


def test_resolution_attempt_base_construction_for_arbitrary_resolver_kind():
    # The base class accepts any ResolverKind — useful for read-side code
    # that needs to handle attempts of yet-to-be-implemented kinds.
    started = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)
    attempt = ResolutionAttempt(
        decision_id="dec-001",
        attempt_id="att-001",
        sequence=0,
        started_at=started,
        completed_at=started,
        resolver_kind=ResolverKind.STEP_UP_AUTH,
        resolver_id="auth-subsystem",
        status=ResolutionStatus.COMPLETED,
        resolution_action=DecisionAction.ALLOW,
        note="MFA challenge passed.",
    )
    assert attempt.resolver_kind == ResolverKind.STEP_UP_AUTH


def test_resolution_attempt_rejects_negative_sequence():
    with pytest.raises(ValidationError):
        ResolutionAttempt(
            decision_id="dec-001",
            attempt_id="att-001",
            sequence=-1,
            started_at=datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
            completed_at=None,
            resolver_kind=ResolverKind.HUMAN,
            resolver_id="user-1",
            status=ResolutionStatus.PENDING,
            resolution_action=None,
            note="",
        )
