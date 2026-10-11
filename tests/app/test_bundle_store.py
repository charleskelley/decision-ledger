"""Behavioral tests for ``app.audit.store.BundleStore`` — multi-tenant dispatch.

The store is the one framework component that must turn a stored row back
into a *reasoner-typed* Observation. These tests pin that contract without
Postgres, using a hand-written fake connection that keeps rows in a dict and
answers ``SELECT reasoner_id, bundle`` the way psycopg would:

- ``write()`` stamps the ``reasoner_id`` column from
  ``raw_event.reasoner_context.reasoner_id``.
- ``load()`` dispatches on that column to the matching ``raw_event_factory``
  and never calls another reasoner's factory.
- A row whose ``reasoner_id`` has no factory raises
  ``UnregisteredReasonerError`` before any factory runs.
- ``replay()`` resolves the registration for the row's ``reasoner_id`` from
  the registry (not from the reconstituted event), passes it to
  ``resolve()``, and a fast-path bundle replays to a match with no diff.
- ``replay()`` of a row whose reasoner has a factory but no registration
  raises ``UnregisteredReasonerError`` and writes no replay log.

The event type is the stub reasoner's ``StubEvent`` (root ``tests/conftest.py``
``observation`` fixture); the two fake tenants ``alpha`` and ``beta`` are the
same shape re-attributed, which is all the store cares about.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

import app.audit.store as store_module
from app.audit.bundle import build_bundle
from app.audit.store import BundleStore
from app.enforcement.resolver import resolve
from app.reasoner_registry import StaticReasonerRegistry
from core.exceptions import UnregisteredReasonerError
from core.observation import ReasonerRegistration
from tests._stub_reasoner.events import StubEvent

if TYPE_CHECKING:
    from core.bundle import DecisionBundle

# ---------------------------------------------------------------------------
# Fake psycopg connection
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Cursor fake that understands the three statements the store issues."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn
        self._row: tuple[str, str] | None = None

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: str, params: tuple[Any, ...] | None = None) -> None:
        assert params is not None
        if "INSERT INTO decisionledger.decision_bundles" in query:
            decision_id, reasoner_id, _entity_id, _created, _action, bundle = params
            self._conn.rows[decision_id] = (reasoner_id, bundle)
        elif "INSERT INTO decisionledger.replay_logs" in query:
            self._conn.replay_logs.append(params)
        elif query.lstrip().startswith("SELECT reasoner_id, bundle"):
            self._row = self._conn.rows.get(params[0])
        else:  # pragma: no cover - guards against an unexpected statement
            raise AssertionError(f"unexpected statement: {query!r}")

    def fetchone(self) -> tuple[str, str] | None:
        return self._row


class _FakeConn:
    """Connection fake: an in-memory ``decision_bundles`` and ``replay_logs``."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, str]] = {}
        self.replay_logs: list[tuple[Any, ...]] = []
        self.commits = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1


# ---------------------------------------------------------------------------
# Factories and registrations
# ---------------------------------------------------------------------------


class _RecordingFactory:
    """Raw-event factory that records every payload it is asked to rebuild."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, data: dict[str, Any]) -> StubEvent:
        self.calls.append(data)
        return StubEvent.model_validate(data, strict=False)


def _forbidden_factory(data: dict[str, Any]) -> StubEvent:
    raise AssertionError("another reasoner's factory must not be called")


def _registration(reasoner_id: str, *, novel_feature: str) -> ReasonerRegistration:
    return ReasonerRegistration(
        reasoner_id=reasoner_id,
        reasoner_name=f"{reasoner_id} (test)",
        allowed_gate_ids=frozenset({"policy"}),
        allowed_prompt_template_ids=frozenset({"stub-v1"}),
        allowed_jurisdictions=frozenset({"REGION_A", "REGION_B"}),
        novel_entity_feature=novel_feature,
    )


ALPHA_REG = _registration("alpha", novel_feature="thin_history")
BETA_REG = _registration("beta", novel_feature="fresh_entity")

_INGESTION_TS = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)
_DECISION_ID = "5b1e0f6a-3c2d-4e8f-9a7b-0c1d2e3f4a5b"


def _event_for(observation: StubEvent, reasoner_id: str) -> StubEvent:
    """Return the fixture event re-attributed to ``reasoner_id``."""
    assert observation.reasoner_context is not None
    ctx = observation.reasoner_context.model_copy(update={"reasoner_id": reasoner_id})
    return observation.model_copy(update={"reasoner_context": ctx})


def _fast_path_bundle(event: Any, registration: ReasonerRegistration) -> DecisionBundle:
    """Build the bundle the framework would persist for a fast-path event.

    ``event`` is ``Any`` because Pydantic ``computed_field`` properties keep
    ``StubEvent`` structurally but not nominally compatible with the
    ``Observation`` protocol (see the ``RawEventFactory`` note in
    ``app/plugin.py``).
    """
    decision = resolve(
        event,
        None,
        snippets=[],
        decision_id=_DECISION_ID,
        registration=registration,
    )
    return build_bundle(
        decision_id=_DECISION_ID,
        obs=event,
        idempotency_key="idem-store-001",
        ingestion_timestamp=_INGESTION_TS,
        enforcement_decision=decision,
    )


def _store(
    conn: _FakeConn,
    *,
    factories: dict[str, Any],
    registrations: list[ReasonerRegistration],
) -> BundleStore:
    return BundleStore(
        conn,  # type: ignore[arg-type]
        raw_event_factories=factories,
        registry=StaticReasonerRegistry(registrations),
    )


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def test_write_stamps_reasoner_id_column_from_reasoner_context(observation):
    conn = _FakeConn()
    store = _store(conn, factories={}, registrations=[ALPHA_REG])

    store.write(_fast_path_bundle(_event_for(observation, "alpha"), ALPHA_REG))

    reasoner_id, _bundle_json = conn.rows[_DECISION_ID]
    assert reasoner_id == "alpha"
    assert conn.commits == 1


# ---------------------------------------------------------------------------
# load — factory dispatch on the reasoner_id column
# ---------------------------------------------------------------------------


def test_load_dispatches_to_the_factory_for_the_rows_reasoner_id(observation):
    conn = _FakeConn()
    alpha_factory = _RecordingFactory()
    store = _store(
        conn,
        factories={"alpha": alpha_factory, "beta": _forbidden_factory},
        registrations=[ALPHA_REG, BETA_REG],
    )
    event = _event_for(observation, "alpha")
    store.write(_fast_path_bundle(event, ALPHA_REG))

    loaded = store.load(_DECISION_ID)

    # _forbidden_factory would have raised if beta's factory were consulted.
    assert len(alpha_factory.calls) == 1
    assert loaded.decision_id == _DECISION_ID
    assert isinstance(loaded.raw_event, StubEvent)
    assert loaded.raw_event.model_dump(mode="json") == event.model_dump(mode="json")


def test_load_raises_unregistered_reasoner_before_calling_any_factory(observation):
    conn = _FakeConn()
    alpha_factory = _RecordingFactory()
    beta_factory = _RecordingFactory()
    store = _store(
        conn,
        factories={"alpha": alpha_factory, "beta": beta_factory},
        registrations=[ALPHA_REG, BETA_REG],
    )
    # The write side does not consult the registry, so a bundle from a
    # reasoner that is not composed in this process can exist in the table.
    ghost_reg = _registration("ghost", novel_feature="thin_history")
    store.write(_fast_path_bundle(_event_for(observation, "ghost"), ghost_reg))

    with pytest.raises(UnregisteredReasonerError) as exc_info:
        store.load(_DECISION_ID)

    assert exc_info.value.reasoner_id == "ghost"
    assert alpha_factory.calls == []
    assert beta_factory.calls == []


def test_load_raises_key_error_when_no_row_exists():
    store = _store(_FakeConn(), factories={}, registrations=[])

    with pytest.raises(KeyError, match="7f000000"):
        store.load("7f000000-0000-4000-8000-000000000000")


def test_load_treats_a_malformed_decision_id_as_missing_without_querying():
    """A non-UUID id is a 'no such bundle', not a database error.

    The column is typed uuid; letting the string reach Postgres would raise a
    DataError and abort the connection's transaction. The store answers with
    the same KeyError callers already handle, and never opens a cursor.
    """

    class _NoCursorConn(_FakeConn):
        def cursor(self) -> _FakeCursor:  # pragma: no cover - must not be hit
            raise AssertionError("malformed id must not reach the database")

    store = _store(_NoCursorConn(), factories={}, registrations=[])

    with pytest.raises(KeyError, match="not-a-uuid"):
        store.load("not-a-uuid")
    with pytest.raises(KeyError, match="not-a-uuid"):
        store.replay("not-a-uuid")


# ---------------------------------------------------------------------------
# replay — registration comes from the registry via the reasoner_id column
# ---------------------------------------------------------------------------


def test_replay_passes_the_registrys_registration_for_the_row_reasoner_id(
    observation, monkeypatch
):
    conn = _FakeConn()
    store = _store(
        conn,
        factories={"alpha": _RecordingFactory(), "beta": _forbidden_factory},
        registrations=[ALPHA_REG, BETA_REG],
    )
    store.write(_fast_path_bundle(_event_for(observation, "alpha"), ALPHA_REG))

    captured: dict[str, Any] = {}

    def _recording_resolve(obs, gate_output, **kwargs):
        captured.update(kwargs)
        return resolve(obs, gate_output, **kwargs)

    monkeypatch.setattr(store_module, "resolve", _recording_resolve)

    result = store.replay(_DECISION_ID)

    assert captured["registration"] is ALPHA_REG
    assert captured["decision_id"] == _DECISION_ID
    assert result.actions_match is True
    assert result.original_action == result.replayed_action == "ALLOW"


def test_replay_of_a_matching_fast_path_bundle_logs_a_match_without_diff(
    observation,
):
    conn = _FakeConn()
    store = _store(
        conn,
        factories={"alpha": _RecordingFactory()},
        registrations=[ALPHA_REG],
    )
    store.write(_fast_path_bundle(_event_for(observation, "alpha"), ALPHA_REG))

    store.replay(_DECISION_ID)

    assert len(conn.replay_logs) == 1
    decision_id, _replayed_at, is_matched, diff = conn.replay_logs[0]
    assert decision_id == _DECISION_ID
    assert is_matched is True
    assert diff is None
    assert conn.commits == 2  # one for write, one for the replay log


def test_replay_raises_unregistered_reasoner_when_registry_lacks_the_row_id(
    observation,
):
    # The factory exists (so load() succeeds) but the registry does not know
    # the reasoner: replay must fail on the registry lookup, not fall back to
    # some other reasoner's enforcement profile.
    conn = _FakeConn()
    store = _store(
        conn,
        factories={"alpha": _RecordingFactory()},
        registrations=[BETA_REG],
    )
    store.write(_fast_path_bundle(_event_for(observation, "alpha"), ALPHA_REG))

    with pytest.raises(UnregisteredReasonerError) as exc_info:
        store.replay(_DECISION_ID)

    assert exc_info.value.reasoner_id == "alpha"
    assert conn.replay_logs == []
