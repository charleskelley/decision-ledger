"""Integration tests for the version-2 ledger — submit, store, replay on Postgres.

Requires PostgreSQL. Run with: make test-integration

Covers the submission contract end to end against the canonical schema,
using the stub reasoner (``tests/_stub_reasoner``) as the tenant and the
scripted fake gates (``tests/_fake_gate``), so no ATO type, model, or LLM is
involved:

- (smoke) a record whose subject a mandatory lookup gate lists is raised to
  the listed action, persisted, and replays to a match.
- (smoke) a gated decision through two gates (a mandatory lookup gate and
  the advisory fake gate) persists both records and replays to a match from
  the stored outputs alone.
- a schema-version-1 row (the pre-submission-contract JSON) is returned by
  lookup and by ``GET /api/v1/decisions/{id}`` as stored, while load and
  replay refuse it — ``POST .../replay`` answers 409
  ``BUNDLE_SCHEMA_UNSUPPORTED``.
- the DDL: ``schema_version`` is ``not null`` with no default, and
  re-applying the DDL is a no-op that does not wait behind an open reader.
- the shared connection: the store leaves no transaction open, and threads
  sharing its connection (the server's threadpool and the event loop) commit
  every write and leave it idle.
- ``recent_recorded`` samples recorded v2 rows scoped by ``since``.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.audit.schema import apply_canonical_schema
from app.audit.store import BundleStore
from app.auth import KEY_HEADER
from app.gates import ComposedGate
from app.main import create_app
from app.reasoner_registry import LiveReasonerRegistry
from app.settings import FrameworkSettings
from app.submit import submit
from core.actions import DecisionAction
from core.exceptions import BundleSchemaUnsupportedError
from core.observation import GateContext
from tests._fake_gate.plugin import FakeGatePlugin, FakeLookupGatePlugin
from tests._memory_ledger import STUB_KEY, TEST_KEYRING
from tests._stub_reasoner.assembler import build_submission
from tests._stub_reasoner.events import StubEvent
from tests._stub_reasoner.plugin import PLUGIN
from tests._stub_reasoner.registry import (
    STUB_REGISTRATION,
    STUB_REGISTRATION_WITH_LOOKUP,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from core.bundle import DecisionBundle
    from core.observation import ReasonerContext

pytestmark = pytest.mark.integration

# Read the DSN from FrameworkSettings so the test honors whatever credentials
# the local .env / docker-compose stack actually used to initialize Postgres.
_PG_DSN = FrameworkSettings().postgres_dsn
_INGESTION_TS = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)

#: The stub registration with a mandatory lookup gate in its gate set.
_WITH_LOOKUP = STUB_REGISTRATION_WITH_LOOKUP


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_conn() -> Iterator[psycopg.Connection[Any]]:
    """An autocommit connection (as the bootstrap opens) with the DDL applied."""
    with psycopg.connect(_PG_DSN, autocommit=True) as conn:
        apply_canonical_schema(conn)
        yield conn


@pytest.fixture
def written_ids(pg_conn: psycopg.Connection[Any]) -> Iterator[list[str]]:
    """Collects decision_ids a test writes; deletes them on teardown."""
    ids: list[str] = []
    yield ids
    if ids:
        with pg_conn.transaction(), pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM decisionledger.replay_logs "
                "WHERE decision_id = ANY(%s::uuid[])",
                (ids,),
            )
            cur.execute(
                "DELETE FROM decisionledger.decision_bundles "
                "WHERE decision_id = ANY(%s::uuid[])",
                (ids,),
            )


def _store(conn: psycopg.Connection[Any], *registrations) -> BundleStore:
    registry = LiveReasonerRegistry(list(registrations or (STUB_REGISTRATION,)))
    return BundleStore(conn, registry=registry)


def _compose(*plugins: Any, mandatory: tuple[str, ...] = ()) -> dict[str, ComposedGate]:
    return {
        p.gate_id: ComposedGate(
            plugin=p,
            gate=p.build(framework=None),  # type: ignore[arg-type]
            mandatory=p.gate_id in mandatory,
        )
        for p in plugins
    }


def _event(widget_id: str | None = None) -> StubEvent:
    return StubEvent(
        event_id=f"evt-{uuid.uuid4()}",
        timestamp=_INGESTION_TS,
        widget_id=widget_id or f"widget-{uuid.uuid4()}",
    )


def _context(reasoner_context: ReasonerContext, score: float) -> ReasonerContext:
    return reasoner_context.model_copy(update={"label_value": score})


# ---------------------------------------------------------------------------
# submit → store → replay
# ---------------------------------------------------------------------------


@pytest.mark.smoke
async def test_a_mandatory_lookup_gate_raises_a_record_and_it_replays(
    pg_conn, written_ids, reasoner_context
):
    event = _event()
    lookup = FakeLookupGatePlugin(listed={event.entity_id: DecisionAction.BLOCK})
    submission = build_submission(
        event,
        _context(reasoner_context, 0.05),
        gate_contexts=[GateContext(gate_id="fake-lookup"), GateContext(gate_id="fake")],
        registration=_WITH_LOOKUP,
    )
    assert (submission.mode, submission.proposed_action) == ("record", "ALLOW")
    store = _store(pg_conn, _WITH_LOOKUP)

    bundle = await submit(
        submission=submission,
        gates=_compose(lookup, FakeGatePlugin()),
        store=store,
        registry=LiveReasonerRegistry([_WITH_LOOKUP]),
        ingestion_timestamp=_INGESTION_TS,
    )
    written_ids.append(bundle.decision_id)

    assert bundle.decision_action == DecisionAction.BLOCK
    assert [(r.gate_id, r.status) for r in bundle.gates] == [
        ("fake-lookup", "ran"),
        ("fake", "skipped"),
    ]
    assert store.load(bundle.decision_id) == bundle
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT schema_version, decision_action FROM "
            "decisionledger.decision_bundles WHERE decision_id = %s",
            (bundle.decision_id,),
        )
        assert cur.fetchone() == (2, "BLOCK")

    result = store.replay(bundle.decision_id)
    assert result.matched
    assert (result.original_action, result.replayed_action) == ("BLOCK", "BLOCK")
    assert result.bypass_rule_holds is True


@pytest.mark.smoke
async def test_a_gated_decision_through_two_gates_round_trips_and_replays(
    pg_conn, written_ids, reasoner_context
):
    event = _event()
    submission = build_submission(
        event,
        _context(reasoner_context, 0.55),
        gate_contexts=[
            GateContext(gate_id="fake-lookup"),
            GateContext(
                gate_id="fake",
                gate_config={
                    "permitted_actions": ["CHALLENGE"],
                    "evidence_jurisdictions": ["REGION_A"],
                },
            ),
        ],
        registration=_WITH_LOOKUP,
    )
    assert submission.mode == "gate"
    store = _store(pg_conn, _WITH_LOOKUP)

    bundle = await submit(
        submission=submission,
        gates=_compose(FakeLookupGatePlugin(), FakeGatePlugin()),
        store=store,
        registry=LiveReasonerRegistry([_WITH_LOOKUP]),
        ingestion_timestamp=_INGESTION_TS,
    )
    written_ids.append(bundle.decision_id)

    assert bundle.decision_action == DecisionAction.CHALLENGE
    assert [(r.gate_id, r.status) for r in bundle.gates] == [
        ("fake-lookup", "ran"),
        ("fake", "ran"),
    ]
    reloaded = store.load(bundle.decision_id)
    assert reloaded == bundle
    fake = reloaded.gate("fake")
    assert fake is not None
    assert fake.output is not None
    assert fake.output.extra == {"scripted": True}

    result = store.replay(bundle.decision_id)
    assert result.matched
    assert [(g.gate_id, g.replayed_action) for g in result.gates] == [
        ("fake-lookup", "ALLOW"),
        ("fake", "CHALLENGE"),
    ]
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT is_matched, diff FROM decisionledger.replay_logs "
            "WHERE decision_id = %s",
            (bundle.decision_id,),
        )
        assert cur.fetchall() == [(True, None)]


async def test_write_is_idempotent_on_decision_id(pg_conn, written_ids, submission):
    store = _store(pg_conn)
    bundle = await submit(
        submission=submission,
        gates={},
        store=store,
        registry=LiveReasonerRegistry([STUB_REGISTRATION]),
        ingestion_timestamp=_INGESTION_TS,
    )
    written_ids.append(bundle.decision_id)
    store.write(bundle)
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM decisionledger.decision_bundles "
            "WHERE decision_id = %s",
            (bundle.decision_id,),
        )
        assert cur.fetchone() == (1,)


# ---------------------------------------------------------------------------
# Schema version 1 rows
# ---------------------------------------------------------------------------

#: A pre-submission-contract bundle as the version-1 store wrote it.
_V1_PAYLOAD: dict[str, Any] = {
    "created_at": "2026-05-01T12:00:00+00:00",
    "raw_event": {
        "event_id": "evt-v1",
        "widget_id": "widget-v1",
        "route": "FAST_PATH_ALLOW",
        "fast_path_rationale": "widget_score=0.150 < 0.20 → FAST_PATH_ALLOW",
    },
    "raw_event_type": "StubEvent",
    "idempotency_key": "evt-v1",
    "ingestion_timestamp": "2026-05-01T12:00:00+00:00",
    "queue_position": None,
    "gate_input": None,
    "gate_output": None,
    "decision_action": "ALLOW",
    "enforcement_rule_applied": None,
    "override_log": ["fast_path_allow: FAST_PATH_ALLOW — policy gate skipped"],
    "latency_breakdown": {"gate_ms": 0.0},
}


@pytest.fixture
def v1_decision_id(pg_conn, written_ids) -> str:
    decision_id = str(uuid.uuid4())
    payload = {**_V1_PAYLOAD, "decision_id": decision_id}
    with pg_conn.transaction(), pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO decisionledger.decision_bundles
                (decision_id, reasoner_id, entity_id, created_at,
                 decision_action, schema_version, bundle)
            VALUES (%s, %s, %s, %s, %s, 1, %s::jsonb)
            """,
            (
                decision_id,
                STUB_REGISTRATION.reasoner_id,
                str(uuid.uuid4()),
                datetime.now(UTC),
                "ALLOW",
                json.dumps(payload),
            ),
        )
    written_ids.append(decision_id)
    return decision_id


def test_a_v1_row_is_looked_up_as_stored_but_not_loaded_or_replayed(
    pg_conn, v1_decision_id
):
    store = _store(pg_conn)
    stored = store.lookup(v1_decision_id)
    assert stored.schema_version == 1
    assert stored.payload["raw_event"]["route"] == "FAST_PATH_ALLOW"
    with pytest.raises(BundleSchemaUnsupportedError):
        store.load(v1_decision_id)
    with pytest.raises(BundleSchemaUnsupportedError):
        store.replay(v1_decision_id)
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM decisionledger.replay_logs WHERE decision_id = %s",
            (v1_decision_id,),
        )
        assert cur.fetchone() == (0,)


def test_the_http_routes_return_a_v1_row_and_refuse_to_replay_it(v1_decision_id):
    app = create_app(
        plugins=[PLUGIN], gate_plugins=[FakeGatePlugin()], keyring=TEST_KEYRING
    )
    headers = {KEY_HEADER: STUB_KEY}
    with TestClient(app) as client:
        got = client.get(f"/api/v1/decisions/{v1_decision_id}", headers=headers)
        assert got.status_code == 200
        body = got.json()
        assert body["schema_version"] == 1
        assert body["decision_action"] == "ALLOW"
        assert body["raw_event"]["route"] == "FAST_PATH_ALLOW"

        replayed = client.post(
            f"/api/v1/decisions/{v1_decision_id}/replay", headers=headers
        )
        assert replayed.status_code == 409
        problem = replayed.json()
        assert problem["code"] == "BUNDLE_SCHEMA_UNSUPPORTED"
        assert (problem["schema_version"], problem["supported_schema_version"]) == (
            1,
            2,
        )


# ---------------------------------------------------------------------------
# DDL and connection hygiene
# ---------------------------------------------------------------------------


def test_schema_version_is_not_null_without_a_default(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT is_nullable, column_default FROM information_schema.columns
            WHERE table_schema = 'decisionledger'
              AND table_name = 'decision_bundles'
              AND column_name = 'schema_version'
            """
        )
        assert cur.fetchone() == ("NO", None)


def test_reapplying_the_ddl_does_not_wait_behind_an_open_reader(pg_conn):
    # A second session sits idle in a transaction holding a read lock on
    # decision_bundles; the guarded DDL must not need an exclusive lock.
    with psycopg.connect(_PG_DSN) as reader:
        with reader.cursor() as cur:
            cur.execute("SELECT count(*) FROM decisionledger.decision_bundles")
        t0 = time.perf_counter()
        apply_canonical_schema(pg_conn)
        assert time.perf_counter() - t0 < 5
        reader.rollback()


async def test_the_store_leaves_no_transaction_open(pg_conn, written_ids, submission):
    store = _store(pg_conn)
    bundle = await submit(
        submission=submission,
        gates={},
        store=store,
        registry=LiveReasonerRegistry([STUB_REGISTRATION]),
        ingestion_timestamp=_INGESTION_TS,
    )
    written_ids.append(bundle.decision_id)
    store.lookup(bundle.decision_id)
    store.replay(bundle.decision_id)
    assert pg_conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


class _CapturingStore:
    """Collects the bundles ``submit`` would write, without writing them."""

    def __init__(self) -> None:
        self.bundles: list[DecisionBundle] = []

    def write(self, bundle: DecisionBundle) -> None:
        self.bundles.append(bundle)


async def test_threads_sharing_the_connection_commit_every_write_and_leave_it_idle(
    pg_conn, written_ids, submission
):
    # The framework's routes share one autocommit connection: lookup and
    # replay run in the server's threadpool while decisions are written from
    # the event loop. Store calls from several threads must not fail, must
    # not leave the connection inside a transaction, and every write must be
    # committed — visible to another session.
    capture = _CapturingStore()
    registry = LiveReasonerRegistry([STUB_REGISTRATION])
    for _ in range(12):
        await submit(
            submission=submission,
            gates={},
            store=capture,  # type: ignore[arg-type]
            registry=registry,
            ingestion_timestamp=_INGESTION_TS,
        )
    bundles = capture.bundles
    written_ids.extend(b.decision_id for b in bundles)
    store = _store(pg_conn)
    store.write(bundles[0])
    errors: list[Exception] = []

    def read() -> None:
        for _ in range(30):
            try:
                store.lookup(bundles[0].decision_id)
            except Exception as err:  # collected, asserted below
                errors.append(err)

    def write() -> None:
        for bundle in bundles[1:]:
            try:
                store.write(bundle)
            except Exception as err:  # collected, asserted below
                errors.append(err)

    threads = [threading.Thread(target=read) for _ in range(6)]
    threads.append(threading.Thread(target=write))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert pg_conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    with psycopg.connect(_PG_DSN) as other, other.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM decisionledger.decision_bundles "
            "WHERE decision_id = ANY(%s::uuid[])",
            ([b.decision_id for b in bundles],),
        )
        assert cur.fetchone() == (len(bundles),)


# ---------------------------------------------------------------------------
# recent_recorded
# ---------------------------------------------------------------------------


async def test_recent_recorded_samples_this_runs_records(
    pg_conn, written_ids, reasoner_context
):
    store = _store(pg_conn)
    registry = LiveReasonerRegistry([STUB_REGISTRATION])
    since = datetime.now(UTC)
    bundles: list[DecisionBundle] = []
    for score in (0.05, 0.10, 0.55):  # two records, one gate request
        submission = build_submission(
            _event(),
            _context(reasoner_context, score),
            gate_contexts=[GateContext(gate_id="fake")],
        )
        bundle = await submit(
            submission=submission,
            gates=_compose(FakeGatePlugin()),
            store=store,
            registry=registry,
            ingestion_timestamp=_INGESTION_TS,
        )
        written_ids.append(bundle.decision_id)
        bundles.append(bundle)

    sample = store.recent_recorded(STUB_REGISTRATION.reasoner_id, limit=10, since=since)
    assert [b.decision_id for b in sample.bundles] == [
        bundles[1].decision_id,
        bundles[0].decision_id,
    ]
    assert sample.unloadable == 0
