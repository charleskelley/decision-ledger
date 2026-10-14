"""Behavioral tests for the policy gate's own DDL and how it is applied.

The gate keeps its tables in its own ``policy_rag`` schema inside the
framework's database (DR-28): its DDL creates the vector extension and that
schema, never touches the framework's tables, and is idempotent so the
plugin and the corpus loader can apply it on every run. Applying it executes
both files in order and commits once; a missing file aborts before anything
runs.

No Postgres is touched: a recording connection stands in.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from policy_rag_gate import schema
from policy_rag_gate.schema import SCHEMA, SQL_DIR, SQL_FILES, apply_schema


class _RecordingConnection:
    """Records every executed statement and commit."""

    def __init__(self) -> None:
        self.executed: list[bytes] = []
        self.commits = 0

    def cursor(self) -> _RecordingConnection:
        return self

    def __enter__(self) -> _RecordingConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, statement: bytes) -> None:
        self.executed.append(statement)

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
