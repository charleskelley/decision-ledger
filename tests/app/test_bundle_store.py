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
- A gate-path bundle round-trips with its universal ``GateOutput`` intact —
  verdict, ``deterministic`` flag, evidence, and the gate's opaque
  ``evidence_snapshot`` / ``extra`` — and replays to a match from the stored
  output alone (no gate call, no corpus), including a deterministic gate's
  tier skips and an evidence-driven jurisdiction HOLD (DR-28).

The event type is the stub reasoner's ``StubEvent`` (root ``tests/conftest.py``
``observation`` fixture); the two fake tenants ``alpha`` and ``beta`` are the
same shape re-attributed, which is all the store cares about.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

import app.audit.store as store_module
from app.audit.bundle import build_bundle
from app.audit.store import BundleStore
from app.enforcement.resolver import resolve
from app.reasoner_registry import StaticReasonerRegistry
from core.actions import DecisionAction
from core.exceptions import UnregisteredReasonerError
from core.gate import EvidenceRef, GateInput, GateOutput, GateVerdict
from core.observation import ReasonerRegistration
from core.routes import GateRoute
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
        jurisdiction_conflict_groups=(
            frozenset({"REGION_A"}),
            frozenset({"REGION_B"}),
        ),
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


def _gate_path_bundle(
    event: Any, registration: ReasonerRegistration, output: GateOutput
) -> DecisionBundle:
    """Build the bundle the framework would persist after a gate ran."""
    routed = event.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )
    decision = resolve(
        routed, output, decision_id=_DECISION_ID, registration=registration
    )
    return build_bundle(
        decision_id=_DECISION_ID,
        obs=routed,
        idempotency_key="idem-store-002",
        ingestion_timestamp=_INGESTION_TS,
        gate_input=GateInput(gate_id=output.gate_id),
        gate_output=output,
        enforcement_decision=decision,
        latency_breakdown={"gate_ms": 12.5},
    )


def _gate_output(
    *,
    gate_id: str = "policy",
    deterministic: bool = False,
    actions: tuple[DecisionAction, ...] = (DecisionAction.CHALLENGE,),
    confidence: float = 0.9,
    jurisdictions: tuple[str, ...] = ("REGION_A",),
) -> GateOutput:
    return GateOutput(
        gate_id=gate_id,
        deterministic=deterministic,
        verdict=GateVerdict(
            gate_id=gate_id,
            permitted_actions=list(actions),
            required_controls=[],
            confidence=confidence,
            escalate_to_human=False,
            escalation_reason=None,
        ),
        evidence=tuple(
            EvidenceRef(
                source_id=f"DOC-{j}", locator="2.1", jurisdictions=frozenset({j})
            )
            for j in jurisdictions
        ),
        evidence_snapshot={
            "retrieval_query": "calibration review",
            "retrieval_results": [{"document_id": "DOC-A", "relevance_score": 0.91}],
            "nested": {"empty": [], "flag": True, "none": None},
        },
        extra={"rationale": "Thin history.", "token_cost": {"total_tokens": 1552}},
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


# ---------------------------------------------------------------------------
# Gate path — the universal GateOutput round-trips and replays on its own
# ---------------------------------------------------------------------------


def test_a_gate_path_bundle_round_trips_with_its_gate_output_intact(observation):
    conn = _FakeConn()
    store = _store(
        conn, factories={"alpha": _RecordingFactory()}, registrations=[ALPHA_REG]
    )
    written = _gate_path_bundle(
        _event_for(observation, "alpha"), ALPHA_REG, _gate_output()
    )
    store.write(written)

    loaded = store.load(_DECISION_ID)

    assert type(loaded.gate_output) is GateOutput
    assert loaded.gate_output == written.gate_output
    assert loaded.gate_input == GateInput(gate_id="policy")
    assert loaded.decision_action == written.decision_action
    assert loaded.override_log == written.override_log
    assert loaded.latency_breakdown == {"gate_ms": 12.5}


def test_the_stored_json_has_no_bundle_level_retrieval_layer(observation):
    conn = _FakeConn()
    store = _store(conn, factories={}, registrations=[ALPHA_REG])
    store.write(
        _gate_path_bundle(_event_for(observation, "alpha"), ALPHA_REG, _gate_output())
    )

    _, bundle_json = conn.rows[_DECISION_ID]
    stored = json.loads(bundle_json)
    assert not set(stored) & {
        "retrieval_query",
        "retrieval_results",
        "retrieval_path",
        "index_version",
    }
    # The gate's own record lives inside its opaque snapshot instead.
    assert stored["gate_output"]["evidence_snapshot"]["retrieval_query"] == (
        "calibration review"
    )


@pytest.mark.parametrize(
    ("output", "expected_action", "expected_rule"),
    [
        (_gate_output(), DecisionAction.CHALLENGE, None),
        (
            _gate_output(jurisdictions=("REGION_A", "REGION_B")),
            DecisionAction.HOLD,
            "tier5_jurisdiction_conflict",
        ),
        (
            _gate_output(
                gate_id="rules",
                deterministic=True,
                actions=(DecisionAction.BLOCK,),
                confidence=0.0,
                jurisdictions=(),
            ),
            DecisionAction.BLOCK,
            None,
        ),
        (
            _gate_output(actions=(DecisionAction.BLOCK,), confidence=0.2),
            DecisionAction.HOLD,
            "tier4_low_confidence",
        ),
    ],
    ids=["accepted", "evidence-conflict", "deterministic-skips", "low-confidence"],
)
def test_a_gate_path_bundle_replays_to_a_match_from_the_stored_output(
    observation, output, expected_action, expected_rule
):
    conn = _FakeConn()
    store = _store(
        conn, factories={"alpha": _RecordingFactory()}, registrations=[ALPHA_REG]
    )
    written = _gate_path_bundle(_event_for(observation, "alpha"), ALPHA_REG, output)
    assert written.decision_action == expected_action
    assert written.enforcement_rule_applied == expected_rule
    store.write(written)

    result = store.replay(_DECISION_ID)

    assert result.actions_match is True
    assert result.replayed_action == expected_action.value
    assert result.enforcement_decision.override_log == written.override_log
    _decision_id, _replayed_at, is_matched, diff = conn.replay_logs[0]
    assert is_matched is True
    assert diff is None


def test_a_replay_that_disagrees_logs_a_mismatch_with_the_diff(observation):
    # Simulate a stored decision that enforcement no longer reproduces (the
    # stored action was CHALLENGE; re-running enforcement yields HOLD).
    conn = _FakeConn()
    store = _store(
        conn, factories={"alpha": _RecordingFactory()}, registrations=[ALPHA_REG]
    )
    store.write(
        _gate_path_bundle(
            _event_for(observation, "alpha"),
            ALPHA_REG,
            _gate_output(actions=(DecisionAction.HOLD,)),
        )
    )
    reasoner_id, bundle_json = conn.rows[_DECISION_ID]
    tampered = json.loads(bundle_json)
    tampered["decision_action"] = "CHALLENGE"
    conn.rows[_DECISION_ID] = (reasoner_id, json.dumps(tampered))

    result = store.replay(_DECISION_ID)

    assert result.actions_match is False
    assert (result.original_action, result.replayed_action) == ("CHALLENGE", "HOLD")
    _decision_id, _replayed_at, is_matched, diff = conn.replay_logs[0]
    assert is_matched is False
    assert json.loads(diff) == {
        "original_action": "CHALLENGE",
        "replayed_action": "HOLD",
        "override_log": list(result.enforcement_decision.override_log),
    }


def test_write_refuses_a_bundle_without_a_reasoner_context(observation):
    conn = _FakeConn()
    store = _store(conn, factories={}, registrations=[ALPHA_REG])
    bundle = _fast_path_bundle(_event_for(observation, "alpha"), ALPHA_REG)
    orphan = bundle.model_copy(
        update={
            "raw_event": bundle.raw_event.model_copy(update={"reasoner_context": None})
        }
    )

    with pytest.raises(ValueError, match="reasoner_context"):
        store.write(orphan)

    assert conn.rows == {}
