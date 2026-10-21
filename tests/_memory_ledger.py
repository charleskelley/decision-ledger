"""In-memory ledger doubles — the framework's HTTP surface without Postgres.

Framework unit tests run with the stub reasoner, the fake gates, and these
doubles (plan section 5): no Docker. Each double subclasses the real class
and replaces only its I/O, so the logic under test (replay, the registration
a bundle is replayed against, the realized action of a resolution chain) is
the production code:

- ``MemoryBundleStore`` keeps rows in a dict and the membership projection
  in a list. ``write`` and ``write_many`` apply the real store's round-trip
  check and open the relationships a new bundle's identity asserts;
  ``replay``, ``registration_for``, and the timeline's walk over the
  projection (``core.identity.linked_entities``) are the production code.
- ``MemoryEntityJournal`` keeps entity events in a list; it checks them as
  the real journal does and rebuilds the store's projection with the
  production fold (``core.identity.project_membership``).
- ``MemorySnapshots`` keeps registration snapshots as canonical JSON and
  reloads them on read, as the table does.
- ``MemoryApiRegistrations`` keeps the registrations written over the API
  (DR-33) in a dict and records each version's snapshot in the store's
  ``MemorySnapshots``, as ``ApiRegistrations`` writes both in one statement;
  ``load`` returns what a restart would read back.
- ``MemoryJournal`` keeps resolution attempts in a list and refuses a
  repeated ``(decision_id, sequence)`` the way the table's unique constraint
  does; ``realized_action`` is inherited.

``memory_framework`` composes them into ``FrameworkServices`` the way the
bootstrap does (the live registry with its API registrations, snapshots, the
identity step with an optional resolver, gates built against the services),
and ``TEST_KEYRING`` holds the keys the API tests authenticate with.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import psycopg

from app.audit.entity_journal import EntityJournal
from app.audit.registrations import (
    ApiRegistrations,
    RegistrationSnapshot,
    RegistrationSnapshots,
)
from app.audit.resolution_journal import ResolutionJournal
from app.audit.store import (
    BundleStore,
    DecisionSummary,
    RecordedSample,
    ReplayResult,
    StoredBundle,
    _row,
    relationship_rows,
)
from app.auth import KeyRing
from app.gates import build_gates
from app.identity import (
    DEFAULT_IDENTITY_BUDGET_MS,
    IdentityService,
    build_resolver,
    declared_namespaces,
)
from app.plugin import FrameworkServices
from app.plugins import build_registry
from app.reasoner_registry import API_SOURCE, RegisteredReasoner
from app.settings import FrameworkSettings
from core.bundle import BUNDLE_SCHEMA_VERSION, DecisionBundle
from core.identity import (
    EntityEvent,
    Membership,
    linked_entities,
    project_membership,
)
from core.observation import (
    ReasonerRegistration,
    canonical_registration,
    registration_version,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from app.gates import GatePlugin
    from app.identity import IdentityResolverPlugin
    from app.plugin import ReasonerPlugin
    from app.reasoner_registry import LiveReasonerRegistry
    from core.resolution import ResolutionAttempt

#: Keys the API tests authenticate with.
STUB_KEY = "stub-reasoner-test-key"
OTHER_KEY = "other-reasoner-test-key"
OPERATOR_KEY = "operator-test-key"
TEST_KEYRING = KeyRing(
    {"stub-reasoner": [STUB_KEY], "other-reasoner": [OTHER_KEY]}, OPERATOR_KEY
)


class MemorySnapshots(RegistrationSnapshots):
    """Registration snapshots in a dict, stored as canonical JSON."""

    def __init__(self) -> None:  # no connection
        self.rows: dict[tuple[str, str], tuple[str, datetime]] = {}

    def record(self, registration: ReasonerRegistration) -> str:
        version = registration_version(registration)
        self.rows.setdefault(
            (registration.reasoner_id, version),
            (json.dumps(canonical_registration(registration)), datetime.now(UTC)),
        )
        return version

    def get(self, reasoner_id: str, version: str) -> RegistrationSnapshot | None:
        row = self.rows.get((reasoner_id, version))
        if row is None:
            return None
        text, first = row
        return RegistrationSnapshot(
            registration=ReasonerRegistration.model_validate_json(text),
            version=version,
            first_registered_at=first,
        )

    def versions(self, reasoner_id: str) -> list[RegistrationSnapshot]:
        found = [
            self.get(rid, version)
            for (rid, version) in sorted(self.rows, key=lambda k: self.rows[k][1])
            if rid == reasoner_id
        ]
        return [snapshot for snapshot in found if snapshot is not None]


class MemoryApiRegistrations(ApiRegistrations):
    """The API registrations in a dict; each save also records its snapshot.

    Attributes:
        rows: ``reasoner_id`` → the entry served now.
        snapshots: The snapshot store each save records into.
    """

    def __init__(self, snapshots: RegistrationSnapshots | None = None) -> None:
        # No connection: the rows live here.
        self.rows: dict[str, RegisteredReasoner] = {}
        self.snapshots = snapshots if snapshots is not None else MemorySnapshots()

    def load(self) -> list[RegisteredReasoner]:
        """The rows a restart would read back, sorted by ``reasoner_id``."""
        return [self.rows[reasoner_id] for reasoner_id in sorted(self.rows)]

    def save(
        self, registration: ReasonerRegistration, *, registered_by: str
    ) -> RegisteredReasoner:
        """Record the version's snapshot, then create or update the row."""
        version = self.snapshots.record(registration)
        now = datetime.now(UTC)
        current = self.rows.get(registration.reasoner_id)
        entry = RegisteredReasoner(
            registration=registration,
            version=version,
            source=API_SOURCE,
            registered_by=registered_by,
            registered_at=current.registered_at if current else now,
            updated_at=now,
        )
        self.rows[registration.reasoner_id] = entry
        return entry


@dataclass
class MemoryRow:
    """One stored bundle row."""

    decision_id: str
    reasoner_id: str
    entity_id: str
    created_at: datetime
    decision_action: str
    schema_version: int
    payload: dict[str, Any]


class MemoryBundleStore(BundleStore):
    """``BundleStore`` with rows in a dict; replay logic is the real one.

    Attributes:
        rows: ``decision_id`` → row.
        memberships: The membership projection (``entity_membership``).
        replays: Every replay result logged, in order.
        write_many_calls: The size of each ``write_many`` batch, in order.
    """

    def __init__(  # no connection
        self,
        registry: LiveReasonerRegistry,
        *,
        snapshots: RegistrationSnapshots | None = None,
    ) -> None:
        self._registry = registry
        self._snapshots = snapshots if snapshots is not None else MemorySnapshots()
        self.rows: dict[str, MemoryRow] = {}
        self.memberships: list[Membership] = []
        self.replays: list[ReplayResult] = []
        self.write_many_calls: list[int] = []

    def _insert(self, row: tuple[object, ...]) -> bool:
        decision_id, reasoner_id, entity_id, created_at, action, version, payload = row
        if str(decision_id) in self.rows:
            return False
        self.rows[str(decision_id)] = MemoryRow(
            decision_id=str(decision_id),
            reasoner_id=str(reasoner_id),
            entity_id=str(entity_id),
            created_at=created_at,  # type: ignore[arg-type]
            decision_action=str(action),
            schema_version=int(version),  # type: ignore[call-overload]
            payload=json.loads(str(payload)),
        )
        return True

    def _relate(self, bundle: DecisionBundle) -> None:
        """Open the bundle's asserted relationships, as the real write does."""
        for subject, related, relationship, since in relationship_rows(bundle):
            link = (uuid.UUID(str(subject)), uuid.UUID(str(related)), relationship)
            if not any(
                m.current
                and (m.subject_entity_id, m.related_entity_id, m.relationship_type)
                == link
                for m in self.memberships
            ):
                self.memberships.append(
                    Membership(
                        subject_entity_id=link[0],
                        related_entity_id=link[1],
                        relationship_type=relationship,  # type: ignore[arg-type]
                        valid_from=since,  # type: ignore[arg-type]
                    )
                )

    def write(self, bundle: DecisionBundle) -> None:
        if self._insert(_row(bundle)):
            self._relate(bundle)

    def write_many(self, bundles: Sequence[DecisionBundle]) -> None:
        rows = [_row(bundle) for bundle in bundles]
        self.write_many_calls.append(len(rows))
        for bundle, row in zip(bundles, rows, strict=True):
            if self._insert(row):
                self._relate(bundle)

    def membership_touching(self, ids: frozenset[uuid.UUID]) -> list[Membership]:
        return [
            m
            for m in self.memberships
            if m.current and (m.subject_entity_id in ids or m.related_entity_id in ids)
        ]

    def insert_raw(
        self,
        payload: dict[str, Any],
        *,
        reasoner_id: str,
        schema_version: int,
        decision_id: str | None = None,
        entity_id: str | None = None,
        decision_action: str = "HOLD",
        created_at: datetime | None = None,
    ) -> str:
        """Store a row as given (an older schema, or one that will not load)."""
        decision_id = decision_id or str(uuid.uuid4())
        self.rows[decision_id] = MemoryRow(
            decision_id=decision_id,
            reasoner_id=reasoner_id,
            entity_id=entity_id or str(uuid.uuid4()),
            created_at=created_at or datetime.now(UTC),
            decision_action=decision_action,
            schema_version=schema_version,
            payload=payload,
        )
        return decision_id

    def lookup(self, decision_id: str) -> StoredBundle:
        try:
            canonical = str(uuid.UUID(decision_id))
        except ValueError:
            raise KeyError(decision_id) from None
        row = self.rows.get(canonical)
        if row is None:
            raise KeyError(decision_id)
        return StoredBundle(
            decision_id=canonical,
            reasoner_id=row.reasoner_id,
            schema_version=row.schema_version,
            payload=json.loads(json.dumps(row.payload)),
        )

    def _log_replay(self, result: ReplayResult) -> None:
        self.replays.append(result)

    def entity_decisions(
        self,
        entity_id: str,
        *,
        reasoner_id: str | None = None,
        limit: int = 50,
        before: datetime | None = None,
        before_id: str | None = None,
        include_related: str | None = None,
    ) -> list[DecisionSummary]:
        try:
            subject = uuid.UUID(entity_id)
            cursor = str(uuid.UUID(before_id)) if before_id is not None else None
        except ValueError:
            raise KeyError(entity_id) from None
        linked = linked_entities(
            subject, self.membership_touching, include_related=include_related
        )
        subjects = {str(i): link for i, link in linked.items()}

        def _after_cursor(row: MemoryRow) -> bool:
            if before is None:
                return True
            if row.created_at < before:
                return True
            return (
                row.created_at == before
                and cursor is not None
                and uuid.UUID(row.decision_id) < uuid.UUID(cursor)
            )

        rows = [
            row
            for row in self.rows.values()
            if row.entity_id in subjects
            and (reasoner_id is None or row.reasoner_id == reasoner_id)
            and _after_cursor(row)
        ]
        rows.sort(key=lambda r: (r.created_at, uuid.UUID(r.decision_id)), reverse=True)
        summaries = []
        for row in rows[:limit]:
            submission = row.payload.get("submission")
            fields = submission if isinstance(submission, dict) else {}
            identity = row.payload.get("identity")
            identity_fields = identity if isinstance(identity, dict) else {}
            summaries.append(
                DecisionSummary(
                    decision_id=row.decision_id,
                    reasoner_id=row.reasoner_id,
                    entity_id=row.entity_id,
                    created_at=row.created_at,
                    decision_action=row.decision_action,
                    schema_version=row.schema_version,
                    mode=fields.get("mode"),
                    event_id=fields.get("event_id"),
                    entity_type=identity_fields.get("entity_type")
                    or fields.get("entity_type"),
                    relationship_type=subjects[row.entity_id],
                )
            )
        return summaries

    def recent_recorded(
        self, reasoner_id: str, *, limit: int, since: datetime | None = None
    ) -> RecordedSample:
        rows = sorted(
            (
                row
                for row in self.rows.values()
                if row.reasoner_id == reasoner_id
                and row.schema_version == BUNDLE_SCHEMA_VERSION
                and row.payload.get("submission", {}).get("mode") == "record"
                and (since is None or row.created_at >= since)
            ),
            key=lambda r: r.created_at,
            reverse=True,
        )[:limit]
        return RecordedSample(
            bundles=tuple(
                DecisionBundle.model_validate_json(json.dumps(row.payload))
                for row in rows
            )
        )

    def bundles(self) -> list[DecisionBundle]:
        """Every version-2 bundle stored, in insertion order."""
        return [
            DecisionBundle.model_validate_json(json.dumps(row.payload))
            for row in self.rows.values()
            if row.schema_version == BUNDLE_SCHEMA_VERSION
        ]


class MemoryEntityJournal(EntityJournal):
    """``EntityJournal`` over a list; checks and the fold are the real ones.

    Attributes:
        appended: Every event appended, in order.
    """

    def __init__(  # no connection
        self, store: MemoryBundleStore, *, relationship_types: frozenset[str]
    ) -> None:
        self._store = store
        self._relationship_types = relationship_types
        self.appended: list[EntityEvent] = []

    def append(
        self, events: Iterable[EntityEvent], *, rebuild: bool = True
    ) -> tuple[Membership, ...] | None:
        self.appended.extend(self.checked(events))
        return self.rebuild_membership() if rebuild else None

    def events(self) -> list[EntityEvent]:
        return list(self.appended)

    def membership(self) -> list[Membership]:
        return list(self._store.memberships)

    def rebuild_membership(self) -> tuple[Membership, ...]:
        rows = sorted(
            (
                row
                for row in self._store.rows.values()
                if row.schema_version == BUNDLE_SCHEMA_VERSION
            ),
            key=lambda r: (r.created_at, uuid.UUID(r.decision_id)),
        )
        identities = [
            DecisionBundle.model_validate_json(json.dumps(row.payload)).identity
            for row in rows
        ]
        projection = project_membership(
            self.appended, [i for i in identities if i is not None and i.related]
        )
        self._store.memberships = list(projection)
        return projection


class MemoryJournal(ResolutionJournal):
    """``ResolutionJournal`` with attempts in a list; ``realized_action`` is real."""

    def __init__(self) -> None:  # no connection
        self.attempts: list[ResolutionAttempt] = []

    def record_attempt(self, attempt: ResolutionAttempt) -> None:
        if any(
            a.decision_id == attempt.decision_id and a.sequence == attempt.sequence
            for a in self.attempts
        ):
            msg = "duplicate key value violates unique constraint"
            raise psycopg.errors.UniqueViolation(msg)
        self.attempts.append(attempt)

    def load_attempts(self, decision_id: str) -> list[ResolutionAttempt]:
        return sorted(
            (a for a in self.attempts if a.decision_id == decision_id),
            key=lambda a: a.sequence,
        )


class NullConnection:
    """Stands in for the framework's Postgres connection; records ``close()``."""

    autocommit = True

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def memory_framework(
    plugins: Iterable[ReasonerPlugin] = (),
    gate_plugins: Iterable[GatePlugin] = (),
    *,
    mandatory: Iterable[str] = (),
    registration_file: Path | None = None,
    journal: ResolutionJournal | None = None,
    resolver_plugin: IdentityResolverPlugin | None = None,
    identity_budget_ms: int = DEFAULT_IDENTITY_BUDGET_MS,
    api_registrations: MemoryApiRegistrations | None = None,
) -> FrameworkServices:
    """Compose in-memory ``FrameworkServices`` the way the bootstrap does.

    Args:
        plugins: Reasoner plugins (their registrations form the registry).
        gate_plugins: Gate plugins, built against the services.
        mandatory: Gate ids the deployment mandates.
        registration_file: A registration file composed with the plugins.
        journal: The resolution journal; a fresh ``MemoryJournal`` by default.
        resolver_plugin: The active identity resolver, built against the
            services; the passthrough when omitted.
        identity_budget_ms: The identity step's budget per resolver call.
        api_registrations: The registrations written over the API, as a
            restart reads them back (pass the one an earlier
            ``memory_framework`` wrote to); a fresh, empty one by default.
            Its snapshots become the store's.

    Returns:
        The services, with a ``MemoryBundleStore`` whose snapshots hold every
        registration the registry serves.
    """
    plugins = tuple(plugins)
    written = (
        api_registrations if api_registrations is not None else MemoryApiRegistrations()
    )
    registry = build_registry(
        plugins,
        registration_file=registration_file,
        written=written.load(),
        writer=written,
    )
    declared_namespaces(entry.registration for entry in registry.entries())
    store = MemoryBundleStore(registry, snapshots=written.snapshots)
    for entry in registry.entries():
        store.snapshots.record(entry.registration)
    services = FrameworkServices(
        settings=FrameworkSettings(),
        pg_conn=NullConnection(),  # type: ignore[arg-type]
        store=store,
        registry=registry,
        reasoners=plugins,
        journal=journal if journal is not None else MemoryJournal(),
    )
    resolver = (
        build_resolver(resolver_plugin, services)
        if resolver_plugin is not None
        else None
    )
    services = dataclasses.replace(
        services, identity=IdentityService(resolver, budget_ms=identity_budget_ms)
    )
    gates = build_gates(list(gate_plugins), services, mandatory=mandatory)
    return dataclasses.replace(services, gates=MappingProxyType(gates))
