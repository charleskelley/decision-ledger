"""Integration tests for BundleStore — live Postgres round trip and replay.

Requires PostgreSQL. Run with: make test-integration

Covers the multi-tenant store contract against the canonical schema, using
the stub reasoner from ``tests/_stub_reasoner`` as the tenant so no ATO type
is involved:

- write → load round trip reconstitutes the bundle through the factory the
  row's ``reasoner_id`` column selects (the stub's ``raw_event_factory``),
  and never through another tenant's factory.
- A row written by a reasoner that is not composed in this process raises
  ``UnregisteredReasonerError`` on load.
- ``write`` is idempotent on ``decision_id``.
- (smoke) replay of a fast-path bundle matches and logs ``is_matched = true``
  with a null ``diff``. No LLM is involved on this path.
- (smoke) a gate-path bundle — a deterministic fake gate's universal
  ``GateOutput`` with evidence and an opaque ``evidence_snapshot`` / ``extra``
  — survives the JSONB round trip unchanged and replays to a match from the
  stored output alone (DR-28).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import psycopg
import pytest

from app.audit.bundle import build_bundle
from app.audit.schema import apply_canonical_schema
from app.audit.store import BundleStore
from app.enforcement.resolver import resolve
from app.reasoner_registry import StaticReasonerRegistry
from app.settings import FrameworkSettings
from core.actions import DecisionAction
from core.exceptions import UnregisteredReasonerError
from core.gate import EvidenceRef, GateInput, GateOutput, GateVerdict
from core.routes import GateRoute
from tests._stub_reasoner.events import StubEvent
from tests._stub_reasoner.plugin import PLUGIN
from tests._stub_reasoner.registry import STUB_REGISTRATION

if TYPE_CHECKING:
    from collections.abc import Iterator

    from core.bundle import DecisionBundle

pytestmark = pytest.mark.integration

# Read the DSN from FrameworkSettings so the test honors whatever credentials
# the local .env / docker-compose stack actually used to initialize Postgres.
_PG_DSN = FrameworkSettings().postgres_dsn

_INGESTION_TS = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)
_STUB = STUB_REGISTRATION.reasoner_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_conn() -> Iterator[psycopg.Connection[Any]]:
    """Open psycopg connection with the canonical schema applied."""
    with psycopg.connect(_PG_DSN) as conn:
        apply_canonical_schema(conn)
        yield conn


@pytest.fixture
def written_ids(pg_conn: psycopg.Connection[Any]) -> Iterator[list[str]]:
    """Collects decision_ids a test writes; deletes them on teardown."""
    ids: list[str] = []
    yield ids
    if ids:
        with pg_conn.cursor() as cur:
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
        pg_conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingFactory:
    """Wraps the stub plugin's factory and counts how often it runs."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, data: dict[str, Any]) -> Any:
        self.calls += 1
        return PLUGIN.raw_event_factory(data)


def _forbidden_factory(data: dict[str, Any]) -> Any:
    raise AssertionError("another reasoner's factory must not be called")


def _fast_path_event(observation: StubEvent, *, reasoner_id: str) -> StubEvent:
    """The fixture observation with fresh ids, attributed to ``reasoner_id``."""
    assert observation.reasoner_context is not None
    ctx = observation.reasoner_context.model_copy(update={"reasoner_id": reasoner_id})
    return observation.model_copy(
        update={
            "event_id": f"evt-{uuid.uuid4().hex[:12]}",
            "widget_id": f"widget-{uuid.uuid4().hex[:8]}",
            "reasoner_context": ctx,
        }
    )


def _fast_path_bundle(event: Any) -> DecisionBundle:
    """Bundle for a fast-path event.

    ``event`` is ``Any`` for the same reason ``RawEventFactory`` returns
    ``Any`` (see ``app/plugin.py``): ``StubEvent`` is structurally but not
    nominally an ``Observation`` under pyright.
    """
    decision_id = str(uuid.uuid4())
    decision = resolve(
        event,
        None,
        decision_id=decision_id,
        registration=STUB_REGISTRATION,
    )
    return build_bundle(
        decision_id=decision_id,
        obs=event,
        idempotency_key=f"idem-{decision_id}",
        ingestion_timestamp=_INGESTION_TS,
        enforcement_decision=decision,
    )


def _stub_store(
    conn: psycopg.Connection[Any], *, stub_factory: Any = PLUGIN.raw_event_factory
) -> BundleStore:
    return BundleStore(
        conn,
        raw_event_factories={
            _STUB: stub_factory,
            "other-reasoner": _forbidden_factory,
        },
        registry=StaticReasonerRegistry([STUB_REGISTRATION]),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_store_round_trips_a_bundle_through_the_row_reasoners_factory(
    pg_conn, written_ids, observation
):
    stub_factory = _RecordingFactory()
    store = _stub_store(pg_conn, stub_factory=stub_factory)
    bundle = _fast_path_bundle(_fast_path_event(observation, reasoner_id=_STUB))
    written_ids.append(bundle.decision_id)

    store.write(bundle)
    loaded = store.load(bundle.decision_id)

    # _forbidden_factory would have raised if dispatch picked the wrong one.
    assert stub_factory.calls == 1
    assert isinstance(loaded.raw_event, StubEvent)
    assert loaded.model_dump(mode="json") == bundle.model_dump(mode="json")


def test_write_stamps_the_reasoner_id_column(pg_conn, written_ids, observation):
    store = _stub_store(pg_conn)
    bundle = _fast_path_bundle(_fast_path_event(observation, reasoner_id=_STUB))
    written_ids.append(bundle.decision_id)

    store.write(bundle)

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT reasoner_id, entity_id::text FROM decisionledger.decision_bundles "
            "WHERE decision_id = %s",
            (bundle.decision_id,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == _STUB
    assert row[1] == str(bundle.raw_event.entity_id)


def test_write_is_idempotent_on_decision_id(pg_conn, written_ids, observation):
    store = _stub_store(pg_conn)
    bundle = _fast_path_bundle(_fast_path_event(observation, reasoner_id=_STUB))
    written_ids.append(bundle.decision_id)

    store.write(bundle)
    store.write(bundle)

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM decisionledger.decision_bundles "
            "WHERE decision_id = %s",
            (bundle.decision_id,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == 1


def test_load_raises_unregistered_reasoner_for_a_row_from_an_uncomposed_reasoner(
    pg_conn, written_ids, observation
):
    # The write side stamps whatever reasoner_id the event carries; a
    # deployment that does not compose that reasoner cannot deserialize it.
    bundle = _fast_path_bundle(
        _fast_path_event(observation, reasoner_id="ghost-reasoner")
    )
    written_ids.append(bundle.decision_id)
    _stub_store(pg_conn).write(bundle)

    with pytest.raises(UnregisteredReasonerError) as exc_info:
        _stub_store(pg_conn).load(bundle.decision_id)

    assert exc_info.value.reasoner_id == "ghost-reasoner"


@pytest.mark.smoke
def test_replay_of_a_fast_path_bundle_matches_and_logs_it(
    pg_conn, written_ids, observation
):
    store = _stub_store(pg_conn)
    bundle = _fast_path_bundle(_fast_path_event(observation, reasoner_id=_STUB))
    written_ids.append(bundle.decision_id)
    store.write(bundle)

    result = store.replay(bundle.decision_id)

    assert result.actions_match is True
    assert result.original_action == result.replayed_action == "ALLOW"
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT is_matched, diff FROM decisionledger.replay_logs "
            "WHERE decision_id = %s",
            (bundle.decision_id,),
        )
        rows = cur.fetchall()
    assert rows == [(True, None)]


@pytest.mark.smoke
def test_a_gate_path_bundle_round_trips_through_jsonb_and_replays(
    pg_conn, written_ids, observation
):
    store = _stub_store(pg_conn)
    event = _fast_path_event(observation, reasoner_id=_STUB).model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    output = GateOutput(
        gate_id="fake-rules",
        deterministic=True,
        verdict=GateVerdict(
            gate_id="fake-rules",
            permitted_actions=[DecisionAction.BLOCK],
            required_controls=["notify_owner"],
            confidence=0.1,
            escalate_to_human=False,
            escalation_reason=None,
        ),
        evidence=(
            EvidenceRef(source_id="ruleset@1.0.0", locator="extreme-score-block"),
        ),
        evidence_snapshot={
            "matched_rule_ids": ["extreme-score-block"],
            "observed": {"label_value": 0.97, "feature_set.flag": True},
            "nested": [{"empty": []}, None, 1.5],
        },
        extra={"note": "opaque to the framework"},
    )
    decision_id = str(uuid.uuid4())
    decision = resolve(
        event, output, decision_id=decision_id, registration=STUB_REGISTRATION
    )
    bundle = build_bundle(
        decision_id=decision_id,
        obs=event,
        idempotency_key=f"idem-{decision_id}",
        ingestion_timestamp=_INGESTION_TS,
        gate_input=GateInput(gate_id="fake-rules"),
        gate_output=output,
        enforcement_decision=decision,
    )
    written_ids.append(decision_id)
    # A deterministic gate skips Tiers 2 and 4, so its low-confidence BLOCK
    # stands; the stored flag must reproduce that on replay.
    assert bundle.decision_action == DecisionAction.BLOCK

    store.write(bundle)
    loaded = store.load(decision_id)
    result = store.replay(decision_id)

    assert loaded.gate_output == output
    assert loaded.model_dump(mode="json") == bundle.model_dump(mode="json")
    assert result.actions_match is True
    assert result.replayed_action == "BLOCK"
    assert result.enforcement_decision.override_log == bundle.override_log
