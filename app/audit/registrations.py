"""Registration snapshots and the registrations written over the API.

A reasoner's registration is data (DR-31): ``registration_version``
content-addresses it, every decision bundle records the version its
registration had when the decision was made, and ``RegistrationSnapshots``
keeps one snapshot per ``(reasoner_id, registration_version)`` in
``decisionledger.reasoner_registrations``. Replay reads the snapshot of the
version a bundle cites, so it enforces against the profile the decision was
made under — not the one registered now, and not only while the reasoner is
still composed.

The bootstrap records every registration the deployment serves before the
first decision; a row is written once (``on conflict do nothing``) and never
updated. Like ``BundleStore``, the snapshots share the framework's
autocommit connection and issue one self-committing statement per call.

``ApiRegistrations`` keeps the registrations written over the API
(``PUT /api/v1/reasoners/{reasoner_id}``, DR-33) in
``decisionledger.reasoner_registry``: one row per reasoner, naming the
version served now, whose registration is that version's snapshot. Saving a
registration writes its snapshot and the row in one statement, so the
ledger never serves, or restarts with, a version it has no snapshot of.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 (dataclass field type)
from typing import TYPE_CHECKING, Any

import structlog

from app.reasoner_registry import API_SOURCE, RegisteredReasoner
from core.observation import (
    ReasonerRegistration,
    canonical_registration,
    registration_version,
)

if TYPE_CHECKING:
    import psycopg

log = structlog.get_logger(__name__)

#: Insert a registration version's snapshot once.
_INSERT_SNAPSHOT = """
    INSERT INTO decisionledger.reasoner_registrations
        (reasoner_id, registration_version, registration)
    VALUES (%s, %s, %s::jsonb)
    ON CONFLICT (reasoner_id, registration_version) DO NOTHING
"""


def _require_autocommit(conn: psycopg.Connection[Any], store: str) -> None:
    if not conn.autocommit:
        msg = (
            f"{store} needs an autocommit connection: each statement it "
            "issues commits on its own"
        )
        raise ValueError(msg)


@dataclass(frozen=True)
class RegistrationSnapshot:
    """One stored registration version.

    Args:
        registration: The registration as it was registered.
        version: Its ``registration_version``.
        first_registered_at: When a deployment first served this version.
    """

    registration: ReasonerRegistration
    version: str
    first_registered_at: datetime


class RegistrationSnapshots:
    """Postgres store of registration snapshots, keyed by reasoner and version.

    Args:
        conn: Open psycopg connection in autocommit mode (the framework's
            shared connection).

    Raises:
        ValueError: The connection is not in autocommit mode.
    """

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        """Bind the store to the framework's autocommit connection."""
        _require_autocommit(conn, "RegistrationSnapshots")
        self._conn = conn
        self._recorded: set[tuple[str, str]] = set()

    def record(self, registration: ReasonerRegistration) -> str:
        """Store the registration's snapshot if its version is new.

        Idempotent: a version already recorded (by this process or any
        earlier one) is left as it is.

        Args:
            registration: The registration to snapshot.

        Returns:
            Its ``registration_version``.
        """
        version = registration_version(registration)
        key = (registration.reasoner_id, version)
        if key in self._recorded:
            return version
        with self._conn.cursor() as cur:
            cur.execute(
                _INSERT_SNAPSHOT,
                (
                    registration.reasoner_id,
                    version,
                    json.dumps(canonical_registration(registration)),
                ),
            )
        self._recorded.add(key)
        log.info(
            "audit.registration_recorded",
            component="audit",
            reasoner_id=registration.reasoner_id,
            registration_version=version,
        )
        return version

    def get(self, reasoner_id: str, version: str) -> RegistrationSnapshot | None:
        """Return the snapshot of one registration version, or ``None``.

        Args:
            reasoner_id: The reasoner.
            version: The ``registration_version`` a bundle cites.

        Returns:
            The snapshot, or ``None`` when that version was never recorded.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT registration, first_registered_at
                FROM decisionledger.reasoner_registrations
                WHERE reasoner_id = %s AND registration_version = %s
                """,
                (reasoner_id, version),
            )
            row = cur.fetchone()
        if row is None:
            return None
        payload, first_registered_at = row
        return RegistrationSnapshot(
            registration=_load(payload),
            version=version,
            first_registered_at=first_registered_at,
        )

    def versions(self, reasoner_id: str) -> list[RegistrationSnapshot]:
        """Every recorded version of a reasoner's registration, oldest first.

        Args:
            reasoner_id: The reasoner.

        Returns:
            Its snapshots in the order they were first registered.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT registration_version, registration, first_registered_at
                FROM decisionledger.reasoner_registrations
                WHERE reasoner_id = %s
                ORDER BY first_registered_at ASC, registration_version ASC
                """,
                (reasoner_id,),
            )
            rows = cur.fetchall()
        return [
            RegistrationSnapshot(
                registration=_load(payload),
                version=str(version),
                first_registered_at=first_registered_at,
            )
            for version, payload, first_registered_at in rows
        ]


class ApiRegistrations:
    """Postgres store of the registrations written over the API (DR-33).

    ``decisionledger.reasoner_registry`` holds one row per reasoner
    registered with ``PUT /api/v1/reasoners/{reasoner_id}``: the version
    served now (a foreign key into the snapshots, which hold the
    registration itself), its source (``api``), who wrote that version, when
    the reasoner was first registered, and when its current version was
    written. Satisfies ``app.reasoner_registry.RegistrationWriter``.

    Args:
        conn: Open psycopg connection in autocommit mode (the framework's
            shared connection); every call issues one self-committing
            statement.

    Raises:
        ValueError: The connection is not in autocommit mode.
    """

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        """Bind the store to the framework's autocommit connection."""
        _require_autocommit(conn, "ApiRegistrations")
        self._conn = conn

    def load(self) -> list[RegisteredReasoner]:
        """Every registration written over the API, as served now.

        Returns:
            One entry per row, sorted by ``reasoner_id``, each holding the
            snapshot of the version its row names.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.reasoner_id, r.registration_version, s.registration,
                       r.source, r.registered_by, r.registered_at, r.updated_at
                FROM decisionledger.reasoner_registry AS r
                JOIN decisionledger.reasoner_registrations AS s
                    USING (reasoner_id, registration_version)
                ORDER BY r.reasoner_id
                """
            )
            rows = cur.fetchall()
        entries: list[RegisteredReasoner] = []
        for reasoner_id, stored, payload, source, by, at, updated in rows:
            registration = _load(payload)
            version = registration_version(registration)
            if version != stored:
                # The snapshot store rewrote a value (jsonb numerics): serve
                # the version of what was reloaded, which intake stamps and
                # the bootstrap snapshots, and say so.
                log.warning(
                    "audit.api_registration_version_drift",
                    component="audit",
                    reasoner_id=reasoner_id,
                    stored_version=stored,
                    registration_version=version,
                )
            entries.append(
                RegisteredReasoner(
                    registration=registration,
                    version=version,
                    source=str(source),
                    registered_by=str(by),
                    registered_at=at,
                    updated_at=updated,
                )
            )
        return entries

    def save(
        self, registration: ReasonerRegistration, *, registered_by: str
    ) -> RegisteredReasoner:
        """Store ``registration`` as its reasoner's API registration.

        One statement writes the version's snapshot (once) and creates or
        updates the reasoner's row, so both are stored or neither: a new row
        records when the reasoner was first registered; an updated one keeps
        that time and records the new version, who wrote it, and when. The
        statement opens no transaction block, since the framework's
        connection is shared across threads (see ``BundleStore``).

        Args:
            registration: The registration to serve from now on.
            registered_by: Who wrote it (a principal label).

        Returns:
            The entry to serve.
        """
        version = registration_version(registration)
        reasoner_id = registration.reasoner_id
        with self._conn.cursor() as cur:
            cur.execute(
                """
                WITH snapshot AS (
                    INSERT INTO decisionledger.reasoner_registrations
                        (reasoner_id, registration_version, registration)
                    VALUES (%(reasoner_id)s, %(version)s, %(registration)s::jsonb)
                    ON CONFLICT (reasoner_id, registration_version) DO NOTHING
                )
                INSERT INTO decisionledger.reasoner_registry
                    (reasoner_id, registration_version, source, registered_by)
                VALUES (%(reasoner_id)s, %(version)s, %(source)s, %(registered_by)s)
                ON CONFLICT (reasoner_id) DO UPDATE SET
                    registration_version = excluded.registration_version,
                    registered_by = excluded.registered_by,
                    updated_at = now()
                RETURNING registered_at, updated_at
                """,
                {
                    "reasoner_id": reasoner_id,
                    "version": version,
                    "registration": json.dumps(canonical_registration(registration)),
                    "source": API_SOURCE,
                    "registered_by": registered_by,
                },
            )
            row = cur.fetchone()
        if row is None:  # RETURNING yields the row written; never empty
            msg = f"the registry row of reasoner {reasoner_id!r} was not written"
            raise RuntimeError(msg)
        registered_at, updated_at = row
        log.info(
            "audit.api_registration_saved",
            component="audit",
            reasoner_id=reasoner_id,
            registration_version=version,
            registered_by=registered_by,
        )
        return RegisteredReasoner(
            registration=registration,
            version=version,
            source=API_SOURCE,
            registered_by=registered_by,
            registered_at=registered_at,
            updated_at=updated_at,
        )


def _load(payload: object) -> ReasonerRegistration:
    """Reload a stored registration (JSONB as a dict, or as text)."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return ReasonerRegistration.model_validate_json(text)
