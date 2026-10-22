"""Apply the policy gate's own DDL shipped in ``policy_rag_gate/sql``.

The gate keeps its tables in its own Postgres schema, ``policy_rag``, inside
the framework's database (DR-28): the framework's DDL creates nothing for it
and the gate's DDL touches nothing of the framework's. The SQL files ship as
package data and are idempotent (``create ... if not exists`` throughout), so
the reference retrieval provider applies them whenever it is built and the
corpus loader before every load. ``01_init.sql`` creates the vector
extension the chunk table needs; the connecting role must be allowed to
create it.

DR-34 keyed the chunks by corpus (``corpus_id``) instead of by reasoner and
added the ``corpora`` and ``documents`` tables. ``create table if not
exists`` cannot change an existing table, so a database that still holds
the earlier chunk table is refused (``LegacySchemaError``) before any
statement runs: drop the gate's schema (``docker compose down -v`` in
development, or ``drop schema policy_rag cascade``) and reload each corpus.

Usage::

    uv run python -m policy_rag_gate.schema    # apply to FrameworkSettings' database
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import structlog

from app.settings import FrameworkSettings

if TYPE_CHECKING:
    from collections.abc import Sequence

log = structlog.get_logger(__name__)

#: The gate's DDL directory (package data, resolved relative to this module
#: so it works from an installed wheel).
SQL_DIR = Path(__file__).resolve().parent / "sql"

#: Files applied, in order: the extension and schema, then the tables.
SQL_FILES: tuple[str, ...] = ("01_init.sql", "02_tables.sql")

#: The gate's Postgres schema.
SCHEMA = "policy_rag"

_CHUNK_COLUMNS = """
    select column_name from information_schema.columns
    where table_schema = 'policy_rag' and table_name = 'policy_chunks'
"""


class LegacySchemaError(RuntimeError):
    """The database holds the gate's chunk table from before DR-34."""


def legacy_schema_problem(conn: psycopg.Connection[Any]) -> str | None:
    """Why the database's ``policy_rag`` schema predates DR-34, or ``None``.

    Args:
        conn: Open connection to the framework's database.

    Returns:
        A message naming the problem and the migration, or ``None`` when the
        chunk table is absent or keyed by ``corpus_id``.
    """
    with conn.cursor() as cur:
        cur.execute(_CHUNK_COLUMNS)
        columns = {str(row[0]) for row in cur.fetchall()}
    if not columns or "corpus_id" in columns:
        return None
    return (
        "policy_rag.policy_chunks predates DR-34: its chunks are keyed by "
        "reasoner_id, not by corpus_id, and the corpora and documents tables "
        "are missing. Drop the gate's schema (docker compose down -v, or "
        "`drop schema policy_rag cascade`), then reload each corpus with "
        "python -m policy_rag_gate.corpus_loader --corpus-id <corpus_id> "
        "(--reasoner-id <reasoner_id> | --corpus-dir <path>), or with "
        "make build-policy-index-<reasoner_id>"
    )


def apply_schema(
    conn: psycopg.Connection[Any],
    sql_dir: Path = SQL_DIR,
    files: Sequence[str] = SQL_FILES,
) -> None:
    """Execute the gate's DDL files against ``conn`` and commit.

    Args:
        conn: Open psycopg connection to the database that holds (or will
            hold) the ``policy_rag`` schema. Autocommit connections work too.
        sql_dir: Directory holding the DDL files.
        files: File names to apply, in order.

    Raises:
        FileNotFoundError: A DDL file is missing. Raised before any statement
            is executed, so a half-present directory never applies a partial
            schema.
        LegacySchemaError: The database holds the chunk table from before
            DR-34. Raised before any statement is executed.
    """
    paths = [sql_dir / name for name in files]
    for path in paths:
        if not path.is_file():
            msg = f"policy gate DDL file not found: {path}"
            raise FileNotFoundError(msg)
    problem = legacy_schema_problem(conn)
    if problem is not None:
        raise LegacySchemaError(problem)
    # psycopg types ``execute`` on ``LiteralString | bytes | sql.SQL``; file
    # contents go in as bytes.
    for path in paths:
        with conn.cursor() as cur:
            cur.execute(path.read_bytes())
    conn.commit()
    log.info(
        "policy_gate.schema_applied",
        component="policy_gate",
        schema=SCHEMA,
        sql_dir=str(sql_dir),
        files=list(files),
    )


def main() -> None:
    """Apply the gate's DDL to the database ``FrameworkSettings`` names."""
    with psycopg.connect(FrameworkSettings().postgres_dsn) as conn:
        apply_schema(conn)


if __name__ == "__main__":
    main()
