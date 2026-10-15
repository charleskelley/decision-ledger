"""Apply the canonical DecisionLedger DDL shipped in ``app/audit/sql``.

The SQL files under ``app/audit/sql/`` are the single source of truth for
the ``decisionledger`` schema. They ship as package data inside the
framework wheel, and docker-compose mounts the same files into the Postgres
init directory. They are idempotent (``create ... if not
exists`` throughout), so applying them on every process start is safe and
replaces the inline DDL that the store and journal used to carry. Statements
that would lock a table on every run (the schema-version upgrade) are guarded
so they run once.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    import psycopg

log = structlog.get_logger(__name__)

#: Default location of the canonical DDL files (package data, resolved
#: relative to this module so it works from an installed wheel).
DEFAULT_SQL_DIR = Path(__file__).resolve().parent / "sql"

#: Files applied, in order. ``01_init.sql`` creates the schema;
#: ``02_tables.sql`` creates every table and index.
SQL_FILES: tuple[str, ...] = ("01_init.sql", "02_tables.sql")


def apply_canonical_schema(
    conn: psycopg.Connection[Any],
    sql_dir: Path = DEFAULT_SQL_DIR,
) -> None:
    """Execute the canonical DDL files against ``conn`` and commit.

    Args:
        conn: Open psycopg connection with privileges to create the
            ``decisionledger`` schema.
        sql_dir: Directory holding ``01_init.sql`` and ``02_tables.sql``.

    Raises:
        FileNotFoundError: A DDL file is missing from ``sql_dir``. Raised
            before any statement is executed, so a half-present directory
            never applies a partial schema.
    """
    paths = [sql_dir / name for name in SQL_FILES]
    for path in paths:
        if not path.is_file():
            msg = f"Canonical DDL file not found: {path}"
            raise FileNotFoundError(msg)
    # psycopg types ``execute`` on ``LiteralString | bytes | sql.SQL``;
    # ``sql.SQL`` also wants a literal, so file contents go in as bytes. One
    # transaction, with a lock timeout scoped to it: a schema change that
    # cannot get its lock (another session holds the table) fails startup
    # loudly instead of hanging it.
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(b"set local lock_timeout = '10s'")
        for path in paths:
            cur.execute(path.read_bytes())
    log.info(
        "audit.schema_applied",
        component="audit",
        sql_dir=str(sql_dir),
        files=list(SQL_FILES),
    )
