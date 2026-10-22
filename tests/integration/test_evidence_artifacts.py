"""Integration tests for the evidence escrow on Postgres (DR-34).

Requires PostgreSQL. Run with: make test-integration

``ArtifactStore`` against the canonical schema: an artifact is escrowed once
under its digest and reads back byte for byte; escrow off writes nothing;
the table's checks pin the key to the content (a hex SHA-256 of the bytes,
their length); and the first decision a row names is no foreign key, since a
gate escrows while it decides, before the bundle exists.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import TYPE_CHECKING, Any

import psycopg
import pytest

from app.audit import ArtifactStore
from app.audit.schema import apply_canonical_schema
from app.settings import FrameworkSettings

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

_PG_DSN = FrameworkSettings().postgres_dsn


@pytest.fixture
def pg_conn() -> Iterator[psycopg.Connection[Any]]:
    """An autocommit connection (as the bootstrap opens) with the DDL applied."""
    with psycopg.connect(_PG_DSN, autocommit=True) as conn:
        apply_canonical_schema(conn)
        yield conn


@pytest.fixture
def escrowed(pg_conn: psycopg.Connection[Any]) -> Iterator[list[str]]:
    """Collects digests a test escrows; deletes their rows on teardown."""
    digests: list[str] = []
    yield digests
    if digests:
        with pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM decisionledger.evidence_artifacts "
                "WHERE artifact_hash = ANY(%s)",
                (digests,),
            )


def _content() -> bytes:
    """Content no other run escrowed (the escrow keeps rows across runs)."""
    return f"# Escrowed\n\nSection 1 applies ({uuid.uuid4()}).\n".encode()


def test_an_artifact_is_escrowed_once_and_reads_back_byte_for_byte(pg_conn, escrowed):
    escrow = ArtifactStore(pg_conn, enabled=True)
    content = _content()
    first_decision = str(uuid.uuid4())

    digest = escrow.put(
        content, media_type="text/markdown", decision_id=first_decision, gate_id="g1"
    )
    assert digest is not None
    escrowed.append(digest)
    again = escrow.put(
        content, media_type="text/plain", decision_id=str(uuid.uuid4()), gate_id="g2"
    )

    assert digest == again == hashlib.sha256(content).hexdigest()
    assert escrow.get(digest) == content
    assert escrow.held([digest, hashlib.sha256(b"absent").hexdigest()]) == {digest}
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT media_type, byte_length, first_decision_id, first_gate_id "
            "FROM decisionledger.evidence_artifacts WHERE artifact_hash = %s",
            (digest,),
        )
        rows = cur.fetchall()
    # One row; the first decision that escrowed it keeps it.
    assert rows == [("text/markdown", len(content), first_decision, "g1")]


def test_the_first_decision_need_not_have_a_bundle(pg_conn, escrowed):
    # A gate escrows while it decides, before the bundle is written (and a
    # decision may be decided without being written at all).
    escrow = ArtifactStore(pg_conn, enabled=True)

    digest = escrow.put(
        _content(), media_type="text/markdown", decision_id="never-written", gate_id="g"
    )

    assert digest is not None
    escrowed.append(digest)
    assert escrow.held([digest]) == {digest}


def test_escrow_off_writes_nothing(pg_conn):
    content = _content()

    digest = ArtifactStore(pg_conn, enabled=False).put(
        content, media_type="text/markdown", decision_id="d", gate_id="g"
    )

    assert digest is None
    assert (
        ArtifactStore(pg_conn, enabled=True).get(hashlib.sha256(content).hexdigest())
        is None
    )


@pytest.mark.parametrize(
    ("artifact_hash", "byte_length"),
    [
        (hashlib.sha256(b"other bytes").hexdigest(), None),  # another content's key
        (None, 3),  # a length that is not the content's
        ("not-a-digest", None),
    ],
)
def test_the_table_pins_the_key_and_length_to_the_content(
    pg_conn, artifact_hash, byte_length
):
    content = _content()
    digest = artifact_hash or hashlib.sha256(content).hexdigest()
    length = byte_length if byte_length is not None else len(content)

    with pytest.raises(psycopg.errors.CheckViolation), pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO decisionledger.evidence_artifacts (artifact_hash, "
            "media_type, byte_length, artifact_bytes, first_decision_id, "
            "first_gate_id) VALUES (%s, %s, %s, %s, %s, %s)",
            (digest, "text/plain", length, content, "d", "g"),
        )
