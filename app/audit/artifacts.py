"""Evidence escrow — the content a gate relied on, kept by the ledger (DR-34).

A gate may hand the ledger the content of a source it read while deciding
(``ArtifactStore.put``): the ledger keeps those bytes once, content-addressed
by their SHA-256 digest, in ``decisionledger.evidence_artifacts``, and the
gate records the digest the store returned on its evidence reference
(``EvidenceRef.artifact_hash``). Later, a gate that verifies its own
references reads its record back (``get``) to prove what a decision relied
on, whatever has happened to the source since; the framework's half of
verify asks only whether each referenced artifact is held (``held``).

The escrow is generic: the framework stores bytes and their media type,
never interprets them, and never deletes a row (retention is the
deployment's records rule). ``DECISIONLEDGER_EVIDENCE_ESCROW=off`` turns
writing off: ``put`` stores nothing and returns ``None``, so a gate records
no ``artifact_hash``. Artifacts escrowed earlier stay readable.

Like ``RegistrationSnapshots``, the store shares the framework's autocommit
connection and issues one self-committing statement per call: ``put`` is
one ``INSERT ... ON CONFLICT DO NOTHING`` (content escrowed before is left
as it is), ``get`` and ``held`` one ``SELECT`` each. Gates call ``put``
while deciding, from a worker thread (``asyncio.to_thread``); psycopg
serializes statements on the shared connection across threads.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterable

    import psycopg

log = structlog.get_logger(__name__)

#: A SHA-256 digest as the escrow keys it: 64 lowercase hex characters.
ARTIFACT_HASH = re.compile(r"[0-9a-f]{64}")

#: Escrow an artifact once; a row already held is left as it is. Returns the
#: key only when this statement inserted the row.
_INSERT_ARTIFACT = """
    INSERT INTO decisionledger.evidence_artifacts
        (artifact_hash, media_type, byte_length, artifact_bytes,
         first_decision_id, first_gate_id)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (artifact_hash) DO NOTHING
    RETURNING artifact_hash
"""

_SELECT_ARTIFACT = """
    SELECT artifact_bytes
    FROM decisionledger.evidence_artifacts
    WHERE artifact_hash = %s
"""

_SELECT_HELD = """
    SELECT artifact_hash
    FROM decisionledger.evidence_artifacts
    WHERE artifact_hash = ANY(%s)
"""


def artifact_hash(content: bytes) -> str:
    """The escrow key of ``content``: its SHA-256 digest, lowercase hex."""
    return hashlib.sha256(content).hexdigest()


class ArtifactStore:
    """Postgres store of escrowed evidence artifacts, keyed by their digest.

    Args:
        conn: Open psycopg connection in autocommit mode (the framework's
            shared connection).
        enabled: Whether ``put`` escrows (``DECISIONLEDGER_EVIDENCE_ESCROW``).
            Reading works either way.

    Raises:
        ValueError: The connection is not in autocommit mode.
    """

    def __init__(self, conn: psycopg.Connection[Any], *, enabled: bool) -> None:
        """Bind the store to the framework's autocommit connection."""
        if not conn.autocommit:
            msg = (
                "ArtifactStore needs an autocommit connection: each statement it "
                "issues commits on its own, on a connection shared across threads"
            )
            raise ValueError(msg)
        self._conn = conn
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        """Whether ``put`` escrows; ``False`` when the deployment turned it off."""
        return self._enabled

    def put(
        self, content: bytes, *, media_type: str, decision_id: str, gate_id: str
    ) -> str | None:
        """Escrow ``content`` (once) and return its digest.

        Idempotent: content escrowed before, by any decision or process, is
        left as it is, and its digest is returned all the same.

        Args:
            content: The bytes the gate relied on.
            media_type: Their media type (``text/markdown``,
                ``application/json``).
            decision_id: The decision the gate is deciding (recorded with a
                new row, as received; not checked against stored bundles).
            gate_id: The gate escrowing it.

        Returns:
            The artifact's digest (``EvidenceRef.artifact_hash``), or ``None``
            when escrow is off and nothing was stored.

        Raises:
            TypeError: ``content`` is not bytes (it cannot be hashed).
            ValueError: ``media_type``, ``decision_id``, or ``gate_id`` is
                blank.
            psycopg.Error: The statement failed. A gate treats it as an
                infrastructure failure and fails closed.
        """
        for name, value in (
            ("media_type", media_type),
            ("decision_id", decision_id),
            ("gate_id", gate_id),
        ):
            if not value.strip():
                msg = f"an escrowed artifact needs a {name}"
                raise ValueError(msg)
        digest = artifact_hash(content)
        if not self._enabled:
            return None
        with self._conn.cursor() as cur:
            cur.execute(
                _INSERT_ARTIFACT,
                (digest, media_type, len(content), content, decision_id, gate_id),
            )
            inserted = cur.fetchone() is not None
        if inserted:
            log.info(
                "audit.artifact_escrowed",
                component="audit",
                artifact_hash=digest,
                media_type=media_type,
                byte_length=len(content),
                decision_id=decision_id,
                gate_id=gate_id,
            )
        return digest

    def get(self, artifact_hash: str) -> bytes | None:
        """The escrowed bytes with digest ``artifact_hash``, or ``None``.

        A string that is not a SHA-256 hex digest is held by no row; it is
        answered ``None`` without a query.

        Args:
            artifact_hash: An ``EvidenceRef.artifact_hash``.

        Returns:
            The artifact's bytes, or ``None`` when the escrow holds none.
        """
        if not ARTIFACT_HASH.fullmatch(artifact_hash):
            return None
        with self._conn.cursor() as cur:
            cur.execute(_SELECT_ARTIFACT, (artifact_hash,))
            row = cur.fetchone()
        return bytes(row[0]) if row is not None else None

    def held(self, artifact_hashes: Iterable[str]) -> frozenset[str]:
        """Which of ``artifact_hashes`` the escrow holds (their bytes unread).

        Args:
            artifact_hashes: Digests to look for; malformed ones are held by
                no row and never queried.

        Returns:
            The digests the escrow holds.
        """
        wanted = sorted({h for h in artifact_hashes if ARTIFACT_HASH.fullmatch(h)})
        if not wanted:
            return frozenset()
        with self._conn.cursor() as cur:
            cur.execute(_SELECT_HELD, (wanted,))
            rows = cur.fetchall()
        return frozenset(str(row[0]) for row in rows)
