"""Registration snapshots — every registration version a decision can cite.

A reasoner's registration is data (DR-31): ``registration_version``
content-addresses it, every decision bundle records the version its
registration had when the decision was made, and this store keeps one
snapshot per ``(reasoner_id, registration_version)`` in
``decisionledger.reasoner_registrations``. Replay reads the snapshot of the
version a bundle cites, so it enforces against the profile the decision was
made under — not the one registered now, and not only while the reasoner is
still composed.

The bootstrap records every registration the deployment serves before the
first decision; a row is written once (``on conflict do nothing``) and never
updated. Like ``BundleStore``, the snapshots share the framework's
autocommit connection and issue one self-committing statement per call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 (dataclass field type)
from typing import TYPE_CHECKING, Any

import structlog

from core.observation import (
    ReasonerRegistration,
    canonical_registration,
    registration_version,
)

if TYPE_CHECKING:
    import psycopg

log = structlog.get_logger(__name__)


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
        if not conn.autocommit:
            msg = (
                "RegistrationSnapshots needs an autocommit connection: each "
                "statement it issues commits on its own"
            )
            raise ValueError(msg)
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
                """
                INSERT INTO decisionledger.reasoner_registrations
                    (reasoner_id, registration_version, registration)
                VALUES (%s, %s, %s::jsonb)
                ON CONFLICT (reasoner_id, registration_version) DO NOTHING
                """,
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


def _load(payload: object) -> ReasonerRegistration:
    """Reload a stored registration (JSONB as a dict, or as text)."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return ReasonerRegistration.model_validate_json(text)
