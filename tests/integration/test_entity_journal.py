"""Integration tests for the identity journal and its projection (DR-32).

Requires PostgreSQL. Run with: make test-integration

Each test runs on a connection of its own inside one transaction that is
rolled back at the end, so the shared local database keeps nothing (the
journal's own transactions become savepoints inside it). Rebuilding the
projection recomputes it from every event and every bundle in the database,
so each test compares only the rows that touch its own fresh ids.

- **The projection is the fold of the journal**: a journal of relates,
  unrelates, merges, and detaches — appended out of time order, with a
  duplicate open and a zero-length interval — rebuilds into exactly the
  hand-built membership table, which is also ``project_membership`` of the
  same events, and the events reload as appended.
- **Decisions populate it too**: a decision whose resolved identity relates
  its subject opens that relationship as it is written, and a rebuild keeps
  it (provenance + journal).
- **Timelines read it on Postgres**: a subject's timeline unions the ids the
  journal merged with it, a group's (``include_related``) its members', and
  a detach takes a merged id off again.
- **Append is all or nothing**: a batch with one event the deployment's
  resolver does not declare appends none.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
import pytest

from app.audit.entity_journal import EntityJournal
from app.audit.schema import apply_canonical_schema
from app.audit.store import BundleStore
from app.gates import ComposedGate
from app.identity import IdentityService, build_resolver
from app.reasoner_registry import StaticReasonerRegistry
from app.settings import FrameworkSettings
from app.submit import submit
from core.identity import (
    EntityEvent,
    JournalProvenance,
    Membership,
    NativeSubject,
    project_membership,
)
from tests._fake_gate.plugin import FakeGatePlugin
from tests._stub_reasoner.registry import STUB_REGISTRATION
from tests._stub_resolver.plugin import FLEET, StubResolverPlugin, fleet_id, owner_id

if TYPE_CHECKING:
    from collections.abc import Iterator

    from core.bundle import DecisionBundle
    from core.submission import Submission

pytestmark = pytest.mark.integration

_PG_DSN = FrameworkSettings().postgres_dsn
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
JOURNAL = JournalProvenance(recorded_by="integration-test", crosswalk_version="cw-1")


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[Any]]:
    """A connection whose work is rolled back when the test ends."""
    with psycopg.connect(_PG_DSN, autocommit=True) as connection:
        apply_canonical_schema(connection)
        with connection.transaction(force_rollback=True):
            yield connection


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _event(
    kind: str,
    subject: uuid.UUID,
    other: uuid.UUID,
    minutes: float,
    relationship: str | None = None,
) -> EntityEvent:
    return EntityEvent(
        event_type=kind,  # type: ignore[arg-type]
        subject_entity_id=subject,
        object_entity_id=other,
        relationship_type=relationship,
        effective_at=_at(minutes),
        provenance=JOURNAL,
    )


def _touching(memberships: list[Membership], ids: set[uuid.UUID]) -> list[Membership]:
    return [
        m
        for m in memberships
        if m.subject_entity_id in ids or m.related_entity_id in ids
    ]


def test_the_rebuilt_projection_equals_the_hand_built_membership_table(conn):
    c1, c2, c3, c4, x, h, h2 = (uuid.uuid4() for _ in range(7))
    events = [
        _event("relate", c1, h, 0, FLEET),
        _event("relate", c2, h, 1, FLEET),
        _event("merge", c1, x, 2),
        _event("unrelate", c2, h, 3, FLEET),
        _event("relate", c2, h2, 4, FLEET),
        _event("detach", x, c1, 5),  # either order ends a merge
        _event("relate", c1, h, 6, FLEET),  # already open: nothing changes
        _event("relate", c3, h, 2.5, FLEET),  # appended late, effective earlier
        _event("relate", c4, h, 7, FLEET),
        _event("unrelate", c4, h, 7, FLEET),  # zero-length: dropped
    ]
    journal = EntityJournal(conn, relationship_types=frozenset({FLEET}))

    journal.append(events[:5], rebuild=False)
    journal.append(events[5:])  # rebuilds

    hand_built = [
        Membership(
            subject_entity_id=c1,
            related_entity_id=h,
            relationship_type=FLEET,
            valid_from=_at(0),
        ),
        Membership(
            subject_entity_id=c1,
            related_entity_id=x,
            relationship_type=None,
            valid_from=_at(2),
            valid_to=_at(5),
        ),
        Membership(
            subject_entity_id=c2,
            related_entity_id=h,
            relationship_type=FLEET,
            valid_from=_at(1),
            valid_to=_at(3),
        ),
        Membership(
            subject_entity_id=c2,
            related_entity_id=h2,
            relationship_type=FLEET,
            valid_from=_at(4),
        ),
        Membership(
            subject_entity_id=c3,
            related_entity_id=h,
            relationship_type=FLEET,
            valid_from=_at(2.5),
        ),
    ]
    ours = {c1, c2, c3, c4, x, h, h2}
    stored = _touching(journal.membership(), ours)
    assert sorted(stored, key=_key) == sorted(hand_built, key=_key)
    assert sorted(stored, key=_key) == sorted(project_membership(events), key=_key)
    assert [e for e in journal.events() if e.subject_entity_id in ours] == events


def _key(m: Membership) -> tuple[str, str, str, datetime]:
    return (
        str(m.subject_entity_id),
        str(m.related_entity_id),
        m.relationship_type or "",
        m.valid_from,
    )


def test_append_is_all_or_nothing(conn):
    a, b = uuid.uuid4(), uuid.uuid4()
    journal = EntityJournal(conn, relationship_types=frozenset({FLEET}))
    with pytest.raises(ValueError, match="does not declare"):
        journal.append(
            [_event("relate", a, b, 0, FLEET), _event("relate", a, b, 1, "x")]
        )
    assert [e for e in journal.events() if e.subject_entity_id == a] == []


# ---------------------------------------------------------------------------
# Decisions populate the projection; timelines read it
# ---------------------------------------------------------------------------


def _resolver_plugin(suffix: str) -> StubResolverPlugin:
    """A stub resolver whose owners and fleet are unique to this test."""
    return StubResolverPlugin(
        crosswalk={
            f"w1-{suffix}": f"owner-a-{suffix}",
            f"w2-{suffix}": f"owner-b-{suffix}",
        },
        fleets={
            f"owner-a-{suffix}": f"fleet-{suffix}",
            f"owner-b-{suffix}": f"fleet-{suffix}",
        },
    )


async def _decide(
    conn: psycopg.Connection[Any],
    submission: Submission,
    native_key: str,
    identity: IdentityService,
) -> DecisionBundle:
    plugin = FakeGatePlugin()
    named = submission.model_copy(
        update={
            "event_id": f"evt-{uuid.uuid4()}",
            "native_subject": NativeSubject(
                native_namespace="widget-registry", native_subject_id=native_key
            ),
        }
    )
    return await submit(
        submission=named,
        gates={"fake": ComposedGate(plugin=plugin, gate=plugin.build(None))},  # type: ignore[arg-type]
        store=BundleStore(conn, registry=StaticReasonerRegistry([STUB_REGISTRATION])),
        registry=StaticReasonerRegistry([STUB_REGISTRATION]),
        ingestion_timestamp=datetime.now(UTC),
        identity=identity,
    )


async def test_decisions_open_their_relationships_and_a_rebuild_keeps_them(
    conn, submission
):
    suffix = uuid.uuid4().hex
    identity = IdentityService(build_resolver(_resolver_plugin(suffix), None))  # type: ignore[arg-type]
    first = await _decide(conn, submission, f"w1-{suffix}", identity)
    await _decide(conn, submission, f"w2-{suffix}", identity)
    owner_a, owner_b = owner_id(f"owner-a-{suffix}"), owner_id(f"owner-b-{suffix}")
    fleet = fleet_id(f"fleet-{suffix}")
    assert first.entity_id == owner_a

    journal = EntityJournal(conn, relationship_types=frozenset({FLEET}))
    ours = {owner_a, owner_b, fleet}
    written = _touching(journal.membership(), ours)
    assert {(m.subject_entity_id, m.related_entity_id) for m in written} == {
        (owner_a, fleet),
        (owner_b, fleet),
    }
    assert all(m.current and m.relationship_type == FLEET for m in written)

    journal.rebuild_membership()
    assert sorted(_touching(journal.membership(), ours), key=_key) == sorted(
        written, key=_key
    )


async def test_timelines_union_merged_ids_and_group_members_on_postgres(
    conn, submission
):
    suffix = uuid.uuid4().hex
    identity = IdentityService(build_resolver(_resolver_plugin(suffix), None))  # type: ignore[arg-type]
    a = await _decide(conn, submission, f"w1-{suffix}", identity)
    b = await _decide(conn, submission, f"w2-{suffix}", identity)
    # A decision the resolver could not match: filed under the reasoner's id.
    stray_submission = submission.model_copy(update={"entity_id": uuid.uuid4()})
    stray = await _decide(conn, stray_submission, f"unknown-{suffix}", identity)
    assert stray.identity is not None
    assert stray.identity.provenance.method == "unresolved"
    owner_a = owner_id(f"owner-a-{suffix}")
    store = BundleStore(conn, registry=StaticReasonerRegistry([STUB_REGISTRATION]))

    def _ids(entity: uuid.UUID, **kw: Any) -> set[str]:
        return {r.decision_id for r in store.entity_decisions(str(entity), **kw)}

    assert _ids(owner_a) == {a.decision_id}
    journal = EntityJournal(conn, relationship_types=frozenset({FLEET}))
    journal.append([_event("merge", owner_a, stray.entity_id, 0)])
    assert _ids(owner_a) == {a.decision_id, stray.decision_id}
    assert _ids(stray.entity_id) == {a.decision_id, stray.decision_id}

    fleet = fleet_id(f"fleet-{suffix}")
    group = store.entity_decisions(str(fleet), include_related=FLEET)
    assert {r.decision_id for r in group} == {
        a.decision_id,
        b.decision_id,
        stray.decision_id,
    }
    assert {r.relationship_type for r in group} == {FLEET}

    journal.append([_event("detach", stray.entity_id, owner_a, 1)])
    assert _ids(owner_a) == {a.decision_id}
