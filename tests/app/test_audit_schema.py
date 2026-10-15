"""Behavioral tests for ``app.audit.schema.apply_canonical_schema``.

The canonical DDL under ``app/audit/sql/`` replaced two inline
``ensure_schema()`` methods. These tests pin how it is applied, using a
recording fake connection so no Postgres is needed:

- Both files are executed, in ``SQL_FILES`` order, with their full contents,
  inside one transaction block that first sets a local lock timeout (a DDL
  statement that cannot get its lock fails startup instead of hanging it).
- A missing file raises ``FileNotFoundError`` before anything is executed.
- The shipped files are idempotent: every ``create`` is ``if not exists``,
  and the one ``alter table`` (the schema-version upgrade) is guarded so a
  current table is never locked.
"""

from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING

import pytest

from app.audit.schema import DEFAULT_SQL_DIR, SQL_FILES, apply_canonical_schema

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# ---------------------------------------------------------------------------
# Recording fake connection
# ---------------------------------------------------------------------------


class _RecordingCursor:
    def __init__(self, conn: _RecordingConn) -> None:
        self._conn = conn

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: bytes) -> None:
        self._conn.events.append(("execute", query))


class _RecordingConn:
    """Records every statement and transaction boundary in order."""

    def __init__(self) -> None:
        self.events: list[tuple[str, bytes | None]] = []

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        self.events.append(("begin", None))
        yield
        self.events.append(("commit", None))


def _write_fake_ddl(sql_dir: Path) -> dict[str, bytes]:
    contents = {
        "01_init.sql": b"create schema if not exists one;\n",
        "02_tables.sql": b"create table if not exists one.two (id int);\n",
    }
    for name, body in contents.items():
        (sql_dir / name).write_bytes(body)
    return contents


# ---------------------------------------------------------------------------
# Behavior
# ---------------------------------------------------------------------------


def test_apply_canonical_schema_executes_both_files_in_declared_order(tmp_path):
    contents = _write_fake_ddl(tmp_path)
    conn = _RecordingConn()

    apply_canonical_schema(conn, sql_dir=tmp_path)  # type: ignore[arg-type]

    executed = [body for kind, body in conn.events if kind == "execute"]
    assert executed[1:] == [contents[name] for name in SQL_FILES]


def test_apply_canonical_schema_runs_in_one_transaction_with_a_lock_timeout(
    tmp_path,
):
    _write_fake_ddl(tmp_path)
    conn = _RecordingConn()

    apply_canonical_schema(conn, sql_dir=tmp_path)  # type: ignore[arg-type]

    kinds = [kind for kind, _ in conn.events]
    assert kinds == ["begin", "execute", "execute", "execute", "commit"]
    assert conn.events[1] == ("execute", b"set local lock_timeout = '10s'")


def test_the_schema_version_upgrade_is_guarded():
    tables = (DEFAULT_SQL_DIR / "02_tables.sql").read_text(encoding="utf-8")
    body = "\n".join(
        line for line in tables.splitlines() if not line.lstrip().startswith("--")
    ).lower()
    assert "alter table" in body
    guard = body.index("if not exists (")
    assert all(match.start() > guard for match in re.finditer("alter table", body))


def test_apply_canonical_schema_raises_before_executing_when_a_file_is_missing(
    tmp_path,
):
    (tmp_path / "01_init.sql").write_bytes(b"create schema if not exists one;\n")
    conn = _RecordingConn()

    with pytest.raises(FileNotFoundError, match=r"02_tables\.sql"):
        apply_canonical_schema(conn, sql_dir=tmp_path)  # type: ignore[arg-type]

    assert conn.events == []


# ---------------------------------------------------------------------------
# The shipped DDL
# ---------------------------------------------------------------------------


def test_default_sql_dir_holds_every_canonical_file():
    for name in SQL_FILES:
        assert (DEFAULT_SQL_DIR / name).is_file(), name


@pytest.mark.parametrize("name", SQL_FILES)
def test_canonical_ddl_is_idempotent(name):
    """Every ``create`` statement is guarded so startup can re-apply the files."""
    text = (DEFAULT_SQL_DIR / name).read_text(encoding="utf-8")
    creates = re.findall(r"^\s*create\s+(?:unique\s+)?\w+(?:\s+\w+)?.*$", text, re.M)

    assert creates, f"{name} declares nothing"
    unguarded = [c for c in creates if "if not exists" not in c]
    assert unguarded == []
