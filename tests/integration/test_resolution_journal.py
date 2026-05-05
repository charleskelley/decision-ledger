"""Integration tests for ResolutionJournal — append-only resolution attempt log.

Requires PostgreSQL. Run with: make test-integration

Covers:
    * record_attempt round-trip (write → load_attempts) with discriminated-union
      reconstruction of typed subclasses.
    * Ordering by ``sequence`` ascending across multiple attempts.
    * realized_action fold across all four cases:
        (a) terminal decision_action returns it directly,
        (b) non-terminal + single terminal attempt → that attempt's action,
        (c) multi-step (ESCALATED then HUMAN ALLOW) → the terminal action,
        (d) non-terminal + only pending attempts → None.
    * resolution_status returns the latest attempt's status, or None when
      no attempts exist.
    * Subclass survival: a HumanResolutionAttempt written and read back
      reconstructs as HumanResolutionAttempt (not the abstract base).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import psycopg
import pytest

from app.audit.resolution_journal import ResolutionJournal
from core.actions import DecisionAction
from core.resolution import (
    HumanResolutionAttempt,
    ResolutionAttempt,
    ResolutionStatus,
    SlaDefaultResolutionAttempt,
)

pytestmark = pytest.mark.integration

_PG_DSN = (
    "postgresql://account_takeover:account_takeover@localhost:5432/account_takeover"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_conn():
    """Open psycopg connection for the journal."""
    with psycopg.connect(_PG_DSN) as conn:
        yield conn


@pytest.fixture
def journal(pg_conn) -> ResolutionJournal:
    j = ResolutionJournal(pg_conn)
    j.ensure_schema()
    return j


@pytest.fixture
def fresh_decision_id(pg_conn) -> str:
    """Unique decision_id per test to avoid cross-test interference.

    Inserts a stub ``decision_bundles`` parent row so the FK constraint on
    ``decision_resolution_attempts.decision_id`` is satisfied. Without this
    parent row, ``record_attempt`` raises ``ForeignKeyViolation`` against
    the canonical schema in ``infra/postgres/02_tables.sql``.
    """
    decision_id = str(uuid.uuid4())
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO decision_bundles
                (decision_id, entity_id, account_id, created_at,
                 decision_action, bundle)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                decision_id,
                str(uuid.uuid4()),
                f"acct-fixture-{decision_id[:8]}",
                datetime.now(UTC),
                "HOLD",
                "{}",
            ),
        )
    pg_conn.commit()
    return decision_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _human_attempt(
    decision_id: str,
    *,
    sequence: int = 0,
    resolver_id: str = "user:reviewer-42",
    status: ResolutionStatus = ResolutionStatus.COMPLETED,
    resolution_action: DecisionAction | None = DecisionAction.ALLOW,
    note: str = "Confirmed legitimate.",
    completed_at: datetime | None = None,
    reviewer_role: str | None = None,
    reference_ticket_id: str | None = None,
) -> HumanResolutionAttempt:
    started = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)
    return HumanResolutionAttempt(
        decision_id=decision_id,
        attempt_id=str(uuid.uuid4()),
        sequence=sequence,
        started_at=started,
        completed_at=completed_at if completed_at is not None else datetime.now(UTC),
        resolver_id=resolver_id,
        status=status,
        resolution_action=resolution_action,
        note=note,
        reviewer_role=reviewer_role,
        reference_ticket_id=reference_ticket_id,
    )


def _sla_default_attempt(
    decision_id: str,
    *,
    sequence: int = 0,
    account_tier: str = "STANDARD",
    sla_window_seconds: int = 86400,
    resolution_action: DecisionAction | None = DecisionAction.BLOCK,
    note: str = "SLA_EXPIRY_DEFAULT",
) -> SlaDefaultResolutionAttempt:
    started = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)
    return SlaDefaultResolutionAttempt(
        decision_id=decision_id,
        attempt_id=str(uuid.uuid4()),
        sequence=sequence,
        started_at=started,
        completed_at=datetime.now(UTC),
        resolver_id="system:sla_timer",
        status=ResolutionStatus.COMPLETED,
        resolution_action=resolution_action,
        note=note,
        account_tier=account_tier,
        sla_window_seconds=sla_window_seconds,
    )


# ---------------------------------------------------------------------------
# Round-trip + subclass survival
# ---------------------------------------------------------------------------


def test_human_attempt_round_trips_as_human_subclass(journal, fresh_decision_id):
    attempt = _human_attempt(
        fresh_decision_id,
        reviewer_role="senior-reviewer",
        reference_ticket_id="JIRA-1234",
    )
    journal.record_attempt(attempt)

    loaded = journal.load_attempts(fresh_decision_id)
    assert len(loaded) == 1
    [reloaded] = loaded
    # Subclass survival — discriminated-union deserialization picks the
    # right concrete type by resolver_kind.
    assert isinstance(reloaded, HumanResolutionAttempt)
    assert reloaded.reviewer_role == "senior-reviewer"
    assert reloaded.reference_ticket_id == "JIRA-1234"


def test_sla_default_attempt_round_trips_as_sla_subclass(journal, fresh_decision_id):
    attempt = _sla_default_attempt(
        fresh_decision_id, account_tier="HIGH_VALUE", sla_window_seconds=7200
    )
    journal.record_attempt(attempt)

    [reloaded] = journal.load_attempts(fresh_decision_id)
    assert isinstance(reloaded, SlaDefaultResolutionAttempt)
    assert reloaded.account_tier == "HIGH_VALUE"
    assert reloaded.sla_window_seconds == 7200


def test_load_attempts_returns_empty_for_unknown_decision(journal):
    assert journal.load_attempts(str(uuid.uuid4())) == []


def test_load_attempts_orders_by_sequence_ascending(journal, fresh_decision_id):
    journal.record_attempt(_human_attempt(fresh_decision_id, sequence=2, note="third"))
    journal.record_attempt(
        _human_attempt(
            fresh_decision_id,
            sequence=0,
            status=ResolutionStatus.ESCALATED,
            resolution_action=None,
            note="first",
        )
    )
    journal.record_attempt(
        _human_attempt(
            fresh_decision_id,
            sequence=1,
            status=ResolutionStatus.ESCALATED,
            resolution_action=None,
            note="second",
        )
    )

    attempts = journal.load_attempts(fresh_decision_id)
    assert [a.sequence for a in attempts] == [0, 1, 2]
    assert [a.note for a in attempts] == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# realized_action — the fold
# ---------------------------------------------------------------------------


def test_realized_action_returns_decision_action_for_terminal_allow(
    journal, fresh_decision_id
):
    assert (
        journal.realized_action(fresh_decision_id, DecisionAction.ALLOW)
        == DecisionAction.ALLOW
    )


def test_realized_action_returns_decision_action_for_terminal_block(
    journal, fresh_decision_id
):
    assert (
        journal.realized_action(fresh_decision_id, DecisionAction.BLOCK)
        == DecisionAction.BLOCK
    )


def test_realized_action_for_hold_resolved_to_allow(journal, fresh_decision_id):
    journal.record_attempt(
        _human_attempt(fresh_decision_id, resolution_action=DecisionAction.ALLOW)
    )
    assert (
        journal.realized_action(fresh_decision_id, DecisionAction.HOLD)
        == DecisionAction.ALLOW
    )


def test_realized_action_for_hold_resolved_via_escalation(journal, fresh_decision_id):
    journal.record_attempt(
        _human_attempt(
            fresh_decision_id,
            sequence=0,
            status=ResolutionStatus.ESCALATED,
            resolution_action=None,
            note="Escalated to senior reviewer.",
        )
    )
    journal.record_attempt(
        _human_attempt(
            fresh_decision_id,
            sequence=1,
            resolution_action=DecisionAction.BLOCK,
            note="Senior reviewer confirmed compromise.",
        )
    )
    assert (
        journal.realized_action(fresh_decision_id, DecisionAction.HOLD)
        == DecisionAction.BLOCK
    )


def test_realized_action_for_hold_with_only_pending_attempt_returns_none(
    journal, fresh_decision_id
):
    journal.record_attempt(
        _human_attempt(
            fresh_decision_id,
            status=ResolutionStatus.PENDING,
            resolution_action=None,
            completed_at=datetime.now(UTC),
        )
    )
    assert journal.realized_action(fresh_decision_id, DecisionAction.HOLD) is None


# ---------------------------------------------------------------------------
# resolution_status
# ---------------------------------------------------------------------------


def test_resolution_status_returns_none_when_no_attempts(journal):
    assert journal.resolution_status(str(uuid.uuid4())) is None


def test_resolution_status_returns_latest_attempt_status(journal, fresh_decision_id):
    journal.record_attempt(
        _human_attempt(
            fresh_decision_id,
            sequence=0,
            status=ResolutionStatus.ESCALATED,
            resolution_action=None,
        )
    )
    assert journal.resolution_status(fresh_decision_id) == ResolutionStatus.ESCALATED

    journal.record_attempt(
        _human_attempt(
            fresh_decision_id,
            sequence=1,
            resolution_action=DecisionAction.ALLOW,
        )
    )
    assert journal.resolution_status(fresh_decision_id) == ResolutionStatus.COMPLETED


# ---------------------------------------------------------------------------
# Append-only contract
# ---------------------------------------------------------------------------


def test_duplicate_sequence_for_same_decision_is_rejected(
    journal, fresh_decision_id, pg_conn
):
    journal.record_attempt(_human_attempt(fresh_decision_id, sequence=0))
    with pytest.raises(psycopg.errors.UniqueViolation):
        journal.record_attempt(_human_attempt(fresh_decision_id, sequence=0))
    pg_conn.rollback()


# Suppress unused-fixture warning for the universal-base import.
_ = ResolutionAttempt
