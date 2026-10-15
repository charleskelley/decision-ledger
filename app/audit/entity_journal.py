"""EntityJournal — the identity journal and its membership projection (DR-32).

The ledger keeps two identity tables next to the bundles, in its own schema
(``app/audit/sql/02_tables.sql``):

- ``entity_events``, the journal: every change to the identity graph
  (``merge``, ``detach``, ``relate``, ``unrelate``), append-only. Who appends
  is the enterprise's business — its resolver tooling when a crosswalk
  version changes, an operator correcting a mapping; the framework stores,
  validates, and folds the events.
- ``entity_membership``, the projection: validity intervals derived from
  the journal and from every relationship a decision's resolved identity
  asserted. ``core.identity.project_membership`` is its definition;
  ``rebuild_membership`` recomputes it from both sources, and the bundle
  store keeps it current as decisions are written (a relationship a new
  decision asserts opens an interval unless one is open).

Use a connection of the journal's own: ``append`` and ``rebuild_membership``
each run in one transaction, which the framework's shared, autocommit
connection never opens (statements of other threads would join it).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import ValidationError

from core.bundle import BUNDLE_SCHEMA_VERSION
from core.identity import EntityEvent, Membership, ResolvedIdentity, project_membership
from core.submission import storage_problem

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    import psycopg

log = structlog.get_logger(__name__)

_INSERT_EVENT = """
    INSERT INTO decisionledger.entity_events
        (event_id, event_type, subject_entity_id, object_entity_id,
         relationship_type, effective_at, provenance)
    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
"""

_INSERT_MEMBERSHIP = """
    INSERT INTO decisionledger.entity_membership
        (subject_entity_id, related_entity_id, relationship_type,
         valid_from, valid_to)
    VALUES (%s, %s, %s, %s, %s)
"""

_SELECT_EVENTS = """
    SELECT event_id, event_type, subject_entity_id, object_entity_id,
           relationship_type, effective_at, provenance
    FROM decisionledger.entity_events
    ORDER BY event_sequence
"""

_SELECT_MEMBERSHIP = """
    SELECT subject_entity_id, related_entity_id, relationship_type,
           valid_from, valid_to
    FROM decisionledger.entity_membership
    ORDER BY subject_entity_id, related_entity_id, relationship_type NULLS FIRST,
             valid_from
"""

#: Identity layers that assert at least one relationship, oldest first.
_SELECT_ASSERTING_IDENTITIES = """
    SELECT decision_id, bundle -> 'identity'
    FROM decisionledger.decision_bundles
    WHERE schema_version = %s
      AND jsonb_typeof(bundle -> 'identity' -> 'related') = 'array'
      AND jsonb_array_length(bundle -> 'identity' -> 'related') > 0
    ORDER BY created_at, decision_id
"""


def membership_row(membership: Membership) -> tuple[object, ...]:
    """The insert parameters of one membership interval."""
    return (
        str(membership.subject_entity_id),
        str(membership.related_entity_id),
        membership.relationship_type,
        membership.valid_from,
        membership.valid_to,
    )


def membership_from_row(row: Sequence[Any]) -> Membership:
    """A membership interval from an ``entity_membership`` row."""
    subject, related, relationship, valid_from, valid_to = row
    return Membership(
        subject_entity_id=subject,
        related_entity_id=related,
        relationship_type=relationship,
        valid_from=valid_from,
        valid_to=valid_to,
    )


class EntityJournal:
    """The append-only identity journal and the projection it feeds.

    Args:
        conn: An open psycopg connection of the journal's own (each append
            and rebuild is one transaction on it).
        relationship_types: The relationship types the deployment's active
            resolver declares; a ``relate`` of any other type is refused
            (an ``unrelate`` may close any type, so a link a former
            resolver emitted can still be ended).
    """

    def __init__(
        self,
        conn: psycopg.Connection[Any],
        *,
        relationship_types: frozenset[str],
    ) -> None:
        """Bind the journal to its connection and the declared types."""
        self._conn = conn
        self._relationship_types = relationship_types

    def append(
        self, events: Iterable[EntityEvent], *, rebuild: bool = True
    ) -> tuple[Membership, ...] | None:
        """Append events to the journal, then rebuild the projection.

        All events are checked before any is written, and they are written
        (with the rebuild) in one transaction: a refused batch writes
        nothing.

        Args:
            events: The events, in the order they are appended.
            rebuild: Rebuild the membership projection in the same
                transaction (default). Pass ``False`` to append several
                batches and rebuild once.

        Returns:
            The rebuilt projection, or ``None`` when ``rebuild`` is off.

        Raises:
            ValueError: A ``relate`` names a relationship type the active
                resolver does not declare, or an event holds a value the
                ledger cannot store.
        """
        batch = self.checked(events)
        rows = [
            (
                str(event.event_id),
                event.event_type,
                str(event.subject_entity_id),
                str(event.object_entity_id),
                event.relationship_type,
                event.effective_at,
                event.provenance.model_dump_json(),
            )
            for event in batch
        ]
        with self._conn.transaction(), self._conn.cursor() as cur:
            if rows:
                cur.executemany(_INSERT_EVENT, rows)
            projection = self._rebuild(cur) if rebuild else None
        log.info(
            "audit.entity_events_appended",
            component="audit",
            count=len(rows),
            rebuilt=rebuild,
        )
        return projection

    def checked(self, events: Iterable[EntityEvent]) -> list[EntityEvent]:
        """Check events before any is appended.

        Args:
            events: The events to append.

        Returns:
            The events, in order.

        Raises:
            ValueError: A ``relate`` names a relationship type the active
                resolver does not declare, or an event holds a value the
                ledger cannot store.
        """
        batch = list(events)
        for event in batch:
            if (
                event.event_type == "relate"
                and event.relationship_type not in self._relationship_types
            ):
                msg = (
                    f"entity event {event.event_id} relates by "
                    f"{event.relationship_type!r}, which the active resolver does "
                    f"not declare (declared: {sorted(self._relationship_types)})"
                )
                raise ValueError(msg)
            problem = storage_problem(event.model_dump(mode="json"))
            if problem is not None:
                msg = f"entity event {event.event_id} holds {problem}"
                raise ValueError(msg)
        return batch

    def events(self) -> list[EntityEvent]:
        """The journal, in append order."""
        with self._conn.cursor() as cur:
            cur.execute(_SELECT_EVENTS)
            return [_event_from_row(row) for row in cur.fetchall()]

    def membership(self) -> list[Membership]:
        """The stored projection, closed and open intervals."""
        with self._conn.cursor() as cur:
            cur.execute(_SELECT_MEMBERSHIP)
            return [membership_from_row(row) for row in cur.fetchall()]

    def rebuild_membership(self) -> tuple[Membership, ...]:
        """Recompute the projection from the journal and the bundles.

        Runs in one transaction that first locks the projection against
        writes, so a decision written meanwhile either is read by the
        rebuild or records its relationships after it.

        Returns:
            The rebuilt projection.
        """
        with self._conn.transaction(), self._conn.cursor() as cur:
            return self._rebuild(cur)

    def _rebuild(self, cur: psycopg.Cursor[Any]) -> tuple[Membership, ...]:
        """Replace the projection with the fold of its sources (in a transaction)."""
        cur.execute(
            "LOCK TABLE decisionledger.entity_membership IN SHARE ROW EXCLUSIVE MODE"
        )
        cur.execute(_SELECT_EVENTS)
        events = [_event_from_row(row) for row in cur.fetchall()]
        cur.execute(_SELECT_ASSERTING_IDENTITIES, (BUNDLE_SCHEMA_VERSION,))
        identities: list[ResolvedIdentity] = []
        for decision_id, payload in cur.fetchall():
            try:
                identities.append(
                    ResolvedIdentity.model_validate_json(
                        payload if isinstance(payload, str) else json.dumps(payload)
                    )
                )
            except ValidationError as err:
                log.warning(
                    "audit.identity_unloadable",
                    component="audit",
                    decision_id=str(decision_id),
                    error=str(err),
                )
        projection = project_membership(events, identities)
        cur.execute("DELETE FROM decisionledger.entity_membership")
        if projection:
            cur.executemany(_INSERT_MEMBERSHIP, [membership_row(m) for m in projection])
        log.info(
            "audit.membership_rebuilt",
            component="audit",
            events=len(events),
            asserting_decisions=len(identities),
            intervals=len(projection),
        )
        return projection


def _event_from_row(row: Sequence[Any]) -> EntityEvent:
    """An entity event from an ``entity_events`` row."""
    (
        event_id,
        event_type,
        subject,
        other,
        relationship,
        effective_at,
        provenance,
    ) = row
    return EntityEvent.model_validate_json(
        json.dumps(
            {
                "event_id": str(event_id),
                "event_type": event_type,
                "subject_entity_id": str(subject),
                "object_entity_id": str(other),
                "relationship_type": relationship,
                "effective_at": effective_at.isoformat(),
                "provenance": (
                    json.loads(provenance)
                    if isinstance(provenance, str)
                    else provenance
                ),
            }
        )
    )


__all__ = ["EntityJournal", "membership_from_row", "membership_row"]
