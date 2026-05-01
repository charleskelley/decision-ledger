"""ResolutionJournal — append-only persistence for ResolutionAttempt rows.

Mirrors the patterns in ``app.audit.store.BundleStore`` (idempotent schema
init, JSON encoding via Pydantic ``model_dump``, structured logging keyed
by ``component``/``decision_id``) but for the resolution surface.

Append-only contract: ``record_attempt`` writes a new row. There is no
update path. New states are recorded as new attempts. The realized action
of a decision is computed at read time by folding the attempt chain via
``realized_action``.

Discriminated-union deserialization: when reading a row, the journal
picks the concrete ``ResolutionAttempt`` subclass by ``resolver_kind``.
MVP closes the union over ``HumanResolutionAttempt`` and
``SlaDefaultResolutionAttempt``; new resolver kinds are added as Union
arms here. The bundle's typed surface (``ResolutionAttempt``) does not
change.

Schema is created in ``infra/postgres/02_tables.sql`` under the
``account_takeover`` schema. ``ensure_schema`` here mirrors that DDL so
the journal can be initialized in a fresh dev database without depending
on the init scripts having run. The ``evidence jsonb`` column is
preserved nullable for backward-compatible reads of pre-DR-21 rows; new
writes leave it null because typed subclass fields supersede it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated, cast

import structlog
from pydantic import Field, TypeAdapter

from core.actions import DecisionAction
from core.resolution import (
    HumanResolutionAttempt,
    ResolutionAttempt,
    ResolutionStatus,
    SlaDefaultResolutionAttempt,
)

if TYPE_CHECKING:
    import psycopg

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Discriminated-union TypeAdapter for deserialization
# ---------------------------------------------------------------------------

_ResolutionAttemptUnion = Annotated[
    HumanResolutionAttempt | SlaDefaultResolutionAttempt,
    Field(discriminator="resolver_kind"),
]
_ATTEMPT_ADAPTER = TypeAdapter(_ResolutionAttemptUnion)


# ---------------------------------------------------------------------------
# DDL — mirrors infra/postgres/02_tables.sql for tests / fresh-DB bootstrapping
# ---------------------------------------------------------------------------

_CREATE_ATTEMPTS_TABLE = """
CREATE TABLE IF NOT EXISTS decision_resolution_attempts (
    attempt_id        TEXT        PRIMARY KEY,
    decision_id       TEXT        NOT NULL,
    sequence          INTEGER     NOT NULL,
    started_at        TIMESTAMPTZ NOT NULL,
    completed_at      TIMESTAMPTZ,
    resolver_kind     TEXT        NOT NULL,
    resolver_id       TEXT        NOT NULL,
    status            TEXT        NOT NULL,
    resolution_action TEXT,
    note              TEXT        NOT NULL,
    evidence          JSONB,
    payload           JSONB       NOT NULL,
    UNIQUE (decision_id, sequence)
);
"""

_CREATE_DECISION_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_resolution_attempts_decision_id "
    "ON decision_resolution_attempts (decision_id);"
)

_CREATE_STATUS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_resolution_attempts_status "
    "ON decision_resolution_attempts (status);"
)


# ---------------------------------------------------------------------------
# ResolutionJournal
# ---------------------------------------------------------------------------


class ResolutionJournal:
    """Append-only persistence for ``ResolutionAttempt`` rows.

    Construct once at process startup with an open psycopg connection.
    Call ``ensure_schema()`` once at startup before the first write — it
    is idempotent.

    The journal is stateless beyond the connection. Thread safety is the
    caller's responsibility (psycopg connections are not thread-safe).

    Args:
        conn: Open psycopg3 synchronous connection.
    """

    def __init__(self, conn: psycopg.Connection[object]) -> None:
        """Initialize ResolutionJournal with a psycopg connection."""
        self._conn = conn

    def ensure_schema(self) -> None:
        """Create decision_resolution_attempts table and indexes if missing.

        Idempotent — safe to call on every startup.
        """
        with self._conn.cursor() as cur:
            cur.execute(_CREATE_ATTEMPTS_TABLE)
            cur.execute(_CREATE_DECISION_INDEX)
            cur.execute(_CREATE_STATUS_INDEX)
        self._conn.commit()

    def record_attempt(self, attempt: ResolutionAttempt) -> None:
        """Append a single ResolutionAttempt row.

        The (decision_id, sequence) pair must be unique. Re-submitting an
        attempt with an existing pair raises a database-level uniqueness
        error rather than silently overwriting — mutation is not permitted.

        The full typed payload (subclass-specific fields included) is
        stored in the ``payload`` JSONB column. Scalar columns are
        denormalized for indexing and operational queries.

        Args:
            attempt: The fully constructed ResolutionAttempt subclass to
                persist (e.g., ``HumanResolutionAttempt``,
                ``SlaDefaultResolutionAttempt``).
        """
        payload_json = attempt.model_dump(mode="json")
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO decision_resolution_attempts
                    (attempt_id, decision_id, sequence, started_at, completed_at,
                     resolver_kind, resolver_id, status, resolution_action,
                     note, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    attempt.attempt_id,
                    attempt.decision_id,
                    attempt.sequence,
                    attempt.started_at,
                    attempt.completed_at,
                    attempt.resolver_kind.value,
                    attempt.resolver_id,
                    attempt.status.value,
                    (
                        attempt.resolution_action.value
                        if attempt.resolution_action is not None
                        else None
                    ),
                    attempt.note,
                    json.dumps(payload_json, default=str),
                ),
            )
        self._conn.commit()
        log.info(
            "audit.resolution_attempt_recorded",
            component="audit",
            decision_id=attempt.decision_id,
            attempt_id=attempt.attempt_id,
            sequence=attempt.sequence,
            resolver_kind=attempt.resolver_kind.value,
            status=attempt.status.value,
        )

    def load_attempts(self, decision_id: str) -> list[ResolutionAttempt]:
        """Load all attempts for a decision, ordered by ``sequence`` ascending.

        Each row is reconstructed into the appropriate concrete subclass
        (``HumanResolutionAttempt`` etc.) via discriminated-union
        deserialization on the ``resolver_kind`` field.

        Args:
            decision_id: Parent decision UUID string.

        Returns:
            All attempts for the decision in sequence order. Empty list
            when no attempts exist.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload
                FROM decision_resolution_attempts
                WHERE decision_id = %s
                ORDER BY sequence ASC
                """,
                (decision_id,),
            )
            rows = cur.fetchall()

        return [_payload_to_attempt(cast("tuple[object, ...]", row)) for row in rows]

    def realized_action(
        self,
        decision_id: str,
        decision_action: DecisionAction,
    ) -> DecisionAction | None:
        """Compute the realized action for a decision.

        Returns ``decision_action`` directly when it is terminal (``ALLOW``
        or ``BLOCK``). For non-terminal ``decision_action`` (``CHALLENGE``
        or ``HOLD``), walks the attempt chain in sequence order and
        returns the first terminal ``resolution_action`` encountered.
        Returns ``None`` when no terminal action has yet been produced.

        Args:
            decision_id: Parent decision UUID string.
            decision_action: The bundle's ``decision_action`` (the
                pipeline's verdict at decision time).

        Returns:
            The terminal action realized on this entity, or ``None`` while
            still pending.
        """
        if decision_action in (DecisionAction.ALLOW, DecisionAction.BLOCK):
            return decision_action

        terminal = (DecisionAction.ALLOW, DecisionAction.BLOCK)
        for attempt in self.load_attempts(decision_id):
            if attempt.resolution_action in terminal:
                return attempt.resolution_action
        return None

    def resolution_status(self, decision_id: str) -> ResolutionStatus | None:
        """Return the status of the most recent attempt for a decision.

        Returns ``None`` when no attempts exist (the decision was never
        opened for resolution). Callers seeing ``None`` for a non-terminal
        decision may interpret it as "pending review."

        Args:
            decision_id: Parent decision UUID string.

        Returns:
            The latest attempt's ``status``, or ``None`` if no attempts exist.
        """
        attempts = self.load_attempts(decision_id)
        if not attempts:
            return None
        return attempts[-1].status


# ---------------------------------------------------------------------------
# Internal — discriminated-union row reconstruction
# ---------------------------------------------------------------------------


def _payload_to_attempt(row: tuple[object, ...]) -> ResolutionAttempt:
    """Reconstruct a typed ResolutionAttempt subclass from the payload column."""
    (payload,) = row
    if isinstance(payload, str):
        data = json.loads(payload)
    elif isinstance(payload, dict):
        data = payload
    else:
        raise TypeError(
            f"Unexpected payload type in resolution_attempts row: {type(payload)!r}"
        )
    return _ATTEMPT_ADAPTER.validate_python(data)
