"""Behavioral tests for the policy gate's own DDL and how it is applied.

The gate keeps its tables in its own ``policy_rag`` schema inside the
framework's database (DR-28): its DDL creates the vector extension and that
schema, never touches the framework's tables, and is idempotent so the
reference provider and the corpus loader can apply it on every run. DR-34
keys the chunks by corpus and adds the ``corpora`` and ``documents`` tables
the reference provider describes and returns documents from; a chunk table
from before it (keyed by ``reasoner_id``) is refused before anything runs.
Applying it executes both files in order and commits once; a missing file
aborts before anything runs.

No Postgres is touched: a recording connection stands in.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from policy_rag_gate import schema
from policy_rag_gate.schema import (
    SCHEMA,
    SQL_DIR,
    SQL_FILES,
    LegacySchemaError,
    apply_schema,
)


class _RecordingConnection:
    """Records every DDL statement and commit; answers the legacy probe.

    Args:
        chunk_columns: The columns ``policy_rag.policy_chunks`` has (empty:
            the table does not exist).
    """

    def __init__(self, chunk_columns: tuple[str, ...] = ()) -> None:
        self.executed: list[bytes] = []
        self.commits = 0
        self._columns = chunk_columns
        self._rows: list[tuple[str]] = []

    def cursor(self) -> _RecordingConnection:
        return self

    def __enter__(self) -> _RecordingConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, statement: bytes | str) -> None:
        if isinstance(statement, str):  # the probe of the chunk table
            self._rows = [(column,) for column in self._columns]
            return
        self.executed.append(statement)

    def fetchall(self) -> list[tuple[str]]:
        return self._rows

    def commit(self) -> None:
        self.commits += 1


def _ddl(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


def test_the_ddl_ships_as_package_data_next_to_the_module():
    assert [p.name for p in sorted(SQL_DIR.glob("*.sql"))] == list(SQL_FILES)


def test_init_creates_the_vector_extension_and_the_gates_own_schema():
    ddl = _ddl("01_init.sql")

    assert re.search(r"create extension if not exists vector;", ddl)
    assert re.search(rf"create schema if not exists {SCHEMA};", ddl)


def test_tables_live_in_the_gates_schema_with_a_matching_embedding_width():
    ddl = _ddl("02_tables.sql")

    assert "create table if not exists policy_rag.corpora" in ddl
    assert "create table if not exists policy_rag.documents" in ddl
    assert "create table if not exists policy_rag.policy_chunks" in ddl
    # all-MiniLM-L6-v2 embeds to 384 dimensions (plugin and loader agree).
    assert "embedding vector(384) not null" in ddl
    for index in re.findall(r"create index if not exists \w+\s+on (\S+)", ddl):
        assert index == "policy_rag.policy_chunks"


@pytest.mark.parametrize("name", SQL_FILES)
def test_every_statement_is_idempotent(name: str):
    code = re.sub(r"--[^\n]*", "", _ddl(name))
    statements = [s.strip() for s in code.split(";") if s.strip()]
    assert statements
    for statement in statements:
        assert "if not exists" in statement, statement


def test_apply_schema_executes_both_files_in_order_and_commits_once():
    conn = _RecordingConnection()

    apply_schema(conn)  # type: ignore[arg-type]

    assert conn.executed == [(SQL_DIR / name).read_bytes() for name in SQL_FILES]
    assert conn.commits == 1


def test_apply_schema_aborts_before_executing_when_a_file_is_missing(tmp_path):
    (tmp_path / "01_init.sql").write_text("select 1;", encoding="utf-8")
    conn = _RecordingConnection()

    with pytest.raises(FileNotFoundError, match=r"02_tables\.sql"):
        apply_schema(conn, sql_dir=tmp_path)  # type: ignore[arg-type]

    assert conn.executed == []
    assert conn.commits == 0


def test_main_applies_the_ddl_to_the_framework_database(monkeypatch):
    conn = _RecordingConnection()
    dsns: list[str] = []

    def _connect(dsn: str) -> Any:
        dsns.append(dsn)
        return conn

    monkeypatch.setenv("POSTGRES_HOST", "db.example")
    monkeypatch.setattr(schema.psycopg, "connect", _connect)

    schema.main()

    assert len(dsns) == 1
    assert "@db.example:" in dsns[0]
    assert len(conn.executed) == len(SQL_FILES)


def test_chunks_are_keyed_by_corpus_with_a_deterministic_text_id():
    ddl = re.sub(r"--[^\n]*", "", _ddl("02_tables.sql"))

    assert "chunk_id text primary key" in ddl
    assert "corpus_id text not null" in ddl
    assert "reasoner_id" not in ddl
    # Every chunk belongs to a stored document version, which belongs to a
    # loaded corpus: deleting a corpus's documents removes its chunks.
    assert (
        "references policy_rag.documents (corpus_id, document_id, document_version)"
        in ddl
    )
    assert "references policy_rag.corpora (corpus_id) on delete cascade" in ddl


@pytest.mark.parametrize(
    "columns",
    [
        ("chunk_id", "reasoner_id", "policy_id", "chunk_text", "embedding"),
        ("chunk_id", "policy_id", "chunk_text"),
    ],
    ids=["keyed-by-reasoner", "no-corpus-column"],
)
def test_a_chunk_table_from_before_dr34_is_refused_before_anything_runs(columns):
    conn = _RecordingConnection(chunk_columns=columns)

    with pytest.raises(LegacySchemaError, match="docker compose down -v"):
        apply_schema(conn)  # type: ignore[arg-type]

    assert conn.executed == []
    assert conn.commits == 0


@pytest.mark.parametrize(
    "columns",
    [(), ("chunk_id", "corpus_id", "policy_id", "chunk_text")],
    ids=["fresh-database", "current-schema"],
)
def test_a_fresh_or_current_schema_is_applied(columns):
    conn = _RecordingConnection(chunk_columns=columns)

    apply_schema(conn)  # type: ignore[arg-type]

    assert len(conn.executed) == len(SQL_FILES)
    assert conn.commits == 1
