"""Behavioral tests for ``app.audit.store.BundleStore`` and pure replay.

The store persists version-2 bundles, reads rows of any schema version, and
replays from a bundle's own cached states. These tests pin that contract
without Postgres, using a hand-written fake connection that keeps rows in a
dict and answers the store's statements the way psycopg would:

- the store needs an autocommit connection and never opens a transaction
  block on it: the connection is shared across threads, and every statement
  commits on its own.
- ``write()`` stamps the tenant, subject, action, and ``schema_version``
  columns from the bundle and refuses a bundle that would not reload
  identically. A failed write leaves the store usable.
- ``lookup()`` returns any row as stored; ``load()`` and ``replay()`` serve
  schema version 2 and refuse older rows (``BundleSchemaUnsupportedError``).
- ``replay()`` enforces against the snapshot of the registration version
  the bundle cites (the registry's registration for an unversioned bundle,
  or when the snapshot is missing but the version is current), re-checks
  every gate and every bypass rule from the bundle, logs a match with a null
  diff, and a disagreement with the diff.
- ``write_many()`` checks every bundle, then sends them in one
  ``executemany``; ``entity_decisions()`` lists a subject's rows, newest
  first, filtered by tenant and paged by ``before``.
- Registration snapshots are written once per version and reload equal.
- ``recent_recorded()`` samples a tenant's recorded v2 bundles, newest first,
  scoped by ``since``, and skips unloadable rows.
- ``replay_bundle`` is pure and reproduces records, gated decisions, failed
  gates, and the competing-rule floor.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from app.audit import build_bundle, replay_bundle
from app.audit.store import BundleStore
from app.enforcement.resolver import resolve_decision, resolve_gate_output
from app.reasoner_registry import StaticReasonerRegistry
from core.actions import DecisionAction
from core.bundle import DecisionBundle, GateRecord
from core.exceptions import (
    BundleSchemaUnsupportedError,
    RegistrationSnapshotMissingError,
    UnregisteredReasonerError,
)
from core.gate import GateInput
from core.observation import registration_version
from core.submission import evaluate_bypass, evaluate_competing_rules
from tests._stub_reasoner.registry import LOW_SCORE_RULE, STUB_REGISTRATION

if TYPE_CHECKING:
    from core.observation import ReasonerRegistration
    from core.submission import Submission

ALLOW, HOLD, BLOCK = DecisionAction.ALLOW, DecisionAction.HOLD, DecisionAction.BLOCK

# ---------------------------------------------------------------------------
# Fake connection
# ---------------------------------------------------------------------------


class _Row:
    def __init__(
        self,
        reasoner_id: str,
        schema_version: int,
        bundle: str,
        created,
        entity_id: str = "",
        action: str = "",
    ):
        self.reasoner_id = reasoner_id
        self.schema_version = schema_version
        self.bundle = bundle
        self.created_at = created
        self.entity_id = entity_id
        self.action = action


class _FakeCursor:
    """Cursor fake that understands exactly the statements the store issues."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: str, params: tuple[Any, ...] | None = None) -> None:
        assert params is not None
        self._conn.statements += 1
        if self._conn.fail_next:
            self._conn.fail_next = False
            raise RuntimeError("simulated database error")
        text = " ".join(query.split())
        if text.startswith("INSERT INTO decisionledger.decision_bundles"):
            decision_id, reasoner_id, entity, created, action, version, bundle = params
            self._conn.rows.setdefault(
                decision_id,
                _Row(reasoner_id, version, bundle, created, str(entity), action),
            )
        elif text.startswith("INSERT INTO decisionledger.reasoner_registrations"):
            reasoner_id, version, registration = params
            self._conn.registrations.setdefault(
                (reasoner_id, version), (json.loads(registration), datetime.now(UTC))
            )
        elif text.startswith("SELECT registration, first_registered_at"):
            found = self._conn.registrations.get((params[0], params[1]))
            self._rows = [] if found is None else [found]
        elif text.startswith("SELECT registration_version, registration"):
            self._rows = [
                (version, payload, first)
                for (rid, version), (payload, first) in sorted(
                    self._conn.registrations.items(), key=lambda kv: kv[1][1]
                )
                if rid == params[0]
            ]
        elif text.startswith("SELECT decision_id, reasoner_id, entity_id, created_at"):
            entity_id, reasoner_id, _same, before, _b2, _b3, cursor, _c2, limit = params
            matches = [
                (decision_id, row)
                for decision_id, row in self._conn.rows.items()
                if row.entity_id == entity_id
                and (reasoner_id is None or row.reasoner_id == reasoner_id)
                and (
                    before is None
                    or row.created_at < before
                    or (
                        row.created_at == before
                        and cursor is not None
                        and decision_id < cursor
                    )
                )
            ]
            matches.sort(key=lambda item: (item[1].created_at, item[0]), reverse=True)
            self._rows = []
            for decision_id, row in matches[:limit]:
                submission = json.loads(row.bundle).get("submission") or {}
                self._rows.append(
                    (
                        decision_id,
                        row.reasoner_id,
                        row.entity_id,
                        row.created_at,
                        row.action,
                        row.schema_version,
                        submission.get("mode"),
                        submission.get("event_id"),
                        submission.get("entity_type"),
                    )
                )
        elif text.startswith("INSERT INTO decisionledger.replay_logs"):
            self._conn.replay_logs.append(params)
        elif text.startswith("SELECT reasoner_id, schema_version, bundle"):
            row = self._conn.rows.get(params[0])
            self._rows = (
                []
                if row is None
                else [(row.reasoner_id, row.schema_version, json.loads(row.bundle))]
            )
        elif text.startswith("SELECT decision_id, bundle FROM decisionledger"):
            reasoner_id, version, since, _since, limit = params
            matches = [
                (decision_id, row)
                for decision_id, row in self._conn.rows.items()
                if row.reasoner_id == reasoner_id
                and row.schema_version == version
                and json.loads(row.bundle).get("submission", {}).get("mode") == "record"
                and (since is None or row.created_at >= since)
            ]
            matches.sort(key=lambda item: item[1].created_at, reverse=True)
            self._rows = [(d, json.loads(r.bundle)) for d, r in matches[:limit]]
        else:  # pragma: no cover - guards against an unexpected statement
            raise AssertionError(f"unexpected statement: {query!r}")

    def executemany(self, query: str, params_seq: list[tuple[Any, ...]]) -> None:
        self._conn.executemany_calls += 1
        for params in params_seq:
            self.execute(query, params)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeConn:
    """An autocommit connection fake with in-memory tables.

    It refuses a transaction block: the framework's connection is shared
    across threads, where transaction blocks interleave and corrupt it.
    """

    def __init__(self, *, autocommit: bool = True) -> None:
        self.autocommit = autocommit
        self.rows: dict[str, _Row] = {}
        self.replay_logs: list[tuple[Any, ...]] = []
        self.registrations: dict[tuple[str, str], tuple[Any, datetime]] = {}
        self.statements = 0
        self.executemany_calls = 0
        self.fail_next = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def transaction(self) -> None:
        raise AssertionError(
            "the store opened a transaction block on its shared connection"
        )


# ---------------------------------------------------------------------------
# Bundles
# ---------------------------------------------------------------------------

_DECISION_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_ID = "22222222-2222-2222-2222-222222222222"


def _record_bundle(
    submission: Submission,
    decision_id: str = _DECISION_ID,
    registration: ReasonerRegistration = STUB_REGISTRATION,
) -> DecisionBundle:
    check = evaluate_bypass(LOW_SCORE_RULE, submission.reasoner_context)
    competing = evaluate_competing_rules(
        registration, LOW_SCORE_RULE, submission.reasoner_context
    )
    records = [
        GateRecord(gate_id="fake", mandatory=False, status="skipped", reason="r")
    ]
    decision = resolve_decision(
        submission=submission,
        records=records,
        registration=registration,
        bypass_check=check,
        competing_checks=competing,
        decision_id=decision_id,
    )
    return build_bundle(
        decision_id=decision_id,
        submission=submission,
        idempotency_key=submission.event_id,
        ingestion_timestamp=submission.timestamp,
        gates=records,
        decision=decision,
        bypass_check=check,
        other_bypass_checks=competing,
    )


def _gated_bundle(
    submission: Submission,
    gate_output: Any,
    *,
    decision_id: str = _DECISION_ID,
    registration: ReasonerRegistration = STUB_REGISTRATION,
    failed: bool = False,
) -> DecisionBundle:
    if failed:
        record = GateRecord(
            gate_id="fake",
            mandatory=False,
            status="failed",
            reason="raised RuntimeError: boom",
            input=GateInput(gate_id="fake"),
            enforcement=resolve_gate_output(
                None,
                registration=registration,
                decision_id=decision_id,
                failure="raised RuntimeError: boom",
            ),
        )
    else:
        record = GateRecord(
            gate_id="fake",
            mandatory=False,
            status="ran",
            input=GateInput(gate_id="fake"),
            output=gate_output,
            enforcement=resolve_gate_output(
                gate_output, registration=registration, decision_id=decision_id
            ),
        )
    decision = resolve_decision(
        submission=submission,
        records=[record],
        registration=registration,
        bypass_check=None,
        decision_id=decision_id,
    )
    return build_bundle(
        decision_id=decision_id,
        submission=submission,
        idempotency_key=submission.event_id,
        ingestion_timestamp=submission.timestamp,
        gates=[record],
        decision=decision,
    )


def _store(conn: _FakeConn, *registrations: ReasonerRegistration) -> BundleStore:
    registry = StaticReasonerRegistry(list(registrations or (STUB_REGISTRATION,)))
    return BundleStore(conn, registry=registry)  # type: ignore[arg-type]


def _insert_v1(conn: _FakeConn, decision_id: str = _OTHER_ID) -> dict[str, Any]:
    payload = {
        "decision_id": decision_id,
        "raw_event": {"event_id": "evt-old", "route": "FAST_PATH_ALLOW"},
        "raw_event_type": "StubEvent",
        "gate_input": None,
        "gate_output": None,
        "decision_action": "ALLOW",
        "enforcement_rule_applied": None,
        "override_log": ["fast_path_allow: ..."],
    }
    conn.rows[decision_id] = _Row(
        "stub-reasoner", 1, json.dumps(payload), datetime.now(UTC)
    )
    return payload


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def test_write_stamps_the_scalar_columns_from_the_bundle(submission):
    conn = _FakeConn()
    bundle = _record_bundle(submission)
    _store(conn).write(bundle)
    row = conn.rows[_DECISION_ID]
    assert row.reasoner_id == "stub-reasoner"
    assert row.schema_version == 2
    assert DecisionBundle.model_validate_json(row.bundle) == bundle
    assert conn.statements == 1


def test_write_is_idempotent_on_decision_id(submission):
    conn = _FakeConn()
    store = _store(conn)
    store.write(_record_bundle(submission))
    store.write(_record_bundle(submission))
    assert len(conn.rows) == 1


def test_write_refuses_a_bundle_that_would_not_reload(submission):
    conn = _FakeConn()
    good = _record_bundle(submission)
    # Bypass validation to build what intake now rejects: a non-finite
    # latency serializes as null and could never be reloaded.
    broken = DecisionBundle.model_construct(
        **{**dict(good), "latency_breakdown": {"gate_ms": float("nan")}}
    )
    with pytest.raises(ValueError, match="does not survive the JSON round trip"):
        _store(conn).write(broken)
    assert conn.rows == {}


def test_a_failed_write_leaves_the_store_usable(submission):
    conn = _FakeConn()
    store = _store(conn)
    conn.fail_next = True
    with pytest.raises(RuntimeError, match="simulated"):
        store.write(_record_bundle(submission))
    assert conn.rows == {}
    store.write(_record_bundle(submission))
    assert _DECISION_ID in conn.rows


# ---------------------------------------------------------------------------
# lookup / load
# ---------------------------------------------------------------------------


def test_lookup_returns_a_v2_row_as_stored(submission):
    conn = _FakeConn()
    store = _store(conn)
    bundle = _record_bundle(submission)
    store.write(bundle)
    stored = store.lookup(_DECISION_ID)
    assert (stored.reasoner_id, stored.schema_version) == ("stub-reasoner", 2)
    assert stored.payload == json.loads(bundle.model_dump_json())
    assert store.load(_DECISION_ID) == bundle


def test_lookup_returns_a_v1_row_but_load_refuses_it():
    conn = _FakeConn()
    payload = _insert_v1(conn)
    store = _store(conn)
    stored = store.lookup(_OTHER_ID)
    assert stored.schema_version == 1
    assert stored.payload == payload
    with pytest.raises(BundleSchemaUnsupportedError) as exc_info:
        store.load(_OTHER_ID)
    assert (exc_info.value.schema_version, exc_info.value.supported) == (1, 2)


def test_lookup_needs_no_registration_for_the_rows_tenant(submission):
    conn = _FakeConn()
    _store(conn).write(_record_bundle(submission))
    other_tenant_only = STUB_REGISTRATION.model_copy(update={"reasoner_id": "other"})
    assert _store(conn, other_tenant_only).load(_DECISION_ID).submission == submission


def test_lookup_raises_key_error_when_no_row_exists():
    with pytest.raises(KeyError):
        _store(_FakeConn()).lookup(_DECISION_ID)


def test_lookup_treats_a_malformed_decision_id_as_missing_without_querying():
    conn = _FakeConn()
    conn.fail_next = True  # any query would raise
    with pytest.raises(KeyError, match="not a UUID"):
        _store(conn).lookup("not-a-uuid")


def test_the_store_refuses_a_connection_that_is_not_autocommit():
    with pytest.raises(ValueError, match="needs an autocommit connection"):
        _store(_FakeConn(autocommit=False))


def test_every_call_is_one_statement_and_none_opens_a_transaction_block(
    submission,
):
    # _FakeConn.transaction() raises: a block on the shared connection would
    # interleave with another thread's.
    conn = _FakeConn()
    store = _store(conn)
    store.write(_record_bundle(submission))
    store.lookup(_DECISION_ID)
    store.load(_DECISION_ID)
    store.recent_recorded("stub-reasoner", limit=5)
    assert conn.statements == 4
    store.replay(_DECISION_ID)  # its lookup, then the replay-log insert
    assert conn.statements == 6


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def test_replay_of_a_record_matches_and_logs_without_diff(submission):
    conn = _FakeConn()
    store = _store(conn)
    store.write(_record_bundle(submission))
    result = store.replay(_DECISION_ID)
    assert result.matched
    assert result.actions_match
    assert (result.original_bypass_rule_holds, result.bypass_rule_holds) == (True, True)
    assert result.competing_rules_match
    [(decision_id, _at, is_matched, diff)] = conn.replay_logs
    assert (decision_id, is_matched, diff) == (_DECISION_ID, True, None)


def test_replay_of_a_gated_bundle_matches_from_the_stored_output(
    gated_submission, gate_output
):
    conn = _FakeConn()
    store = _store(conn)
    store.write(_gated_bundle(gated_submission, gate_output))
    result = store.replay(_DECISION_ID)
    assert result.matched
    assert [
        (g.gate_id, g.original_action, g.replayed_action) for g in result.gates
    ] == [("fake", "ALLOW", "ALLOW")]


def test_replay_of_a_v1_row_is_refused_and_logs_nothing():
    conn = _FakeConn()
    _insert_v1(conn)
    with pytest.raises(BundleSchemaUnsupportedError):
        _store(conn).replay(_OTHER_ID)
    assert conn.replay_logs == []


def test_replay_needs_the_rows_tenant_registered(submission):
    conn = _FakeConn()
    _store(conn).write(_record_bundle(submission))
    other_tenant_only = STUB_REGISTRATION.model_copy(update={"reasoner_id": "other"})
    with pytest.raises(UnregisteredReasonerError):
        _store(conn, other_tenant_only).replay(_DECISION_ID)
    assert conn.replay_logs == []


def test_an_unversioned_bundle_replays_against_the_current_registration(
    gated_submission, gate_output
):
    # A thin-history widget held by Tier 3 at decision time, in a bundle that
    # records no registration version; replayed against a registration
    # without the novel-entity feature it no longer holds, and the store
    # reports the mismatch.
    rc = gated_submission.reasoner_context
    novel = gated_submission.model_copy(
        update={
            "reasoner_context": rc.model_copy(
                update={"feature_set": {**rc.feature_set, "thin_history": True}}
            )
        }
    )
    conn = _FakeConn()
    _store(conn).write(_gated_bundle(novel, gate_output))
    no_tier3 = STUB_REGISTRATION.model_copy(update={"novel_entity_feature": None})
    result = _store(conn, no_tier3).replay(_DECISION_ID)
    assert (result.original_action, result.replayed_action) == ("HOLD", "ALLOW")
    assert not result.matched
    [(_id, _at, is_matched, diff)] = conn.replay_logs
    assert is_matched is False
    assert json.loads(diff)["replayed_action"] == "ALLOW"


def test_a_tampered_row_replays_to_a_mismatch_with_the_diff(submission):
    conn = _FakeConn()
    store = _store(conn)
    store.write(_record_bundle(submission))
    row = conn.rows[_DECISION_ID]
    payload = json.loads(row.bundle)
    payload["decision_action"] = "BLOCK"
    row.bundle = json.dumps(payload)
    result = store.replay(_DECISION_ID)
    assert (result.original_action, result.replayed_action) == ("BLOCK", "ALLOW")
    diff = json.loads(conn.replay_logs[0][3])
    assert diff["original_action"] == "BLOCK"
    assert diff["override_log"][-1] == (
        "combinator: most_restrictive(proposed=ALLOW) → ALLOW"
    )


# ---------------------------------------------------------------------------
# recent_recorded
# ---------------------------------------------------------------------------


def test_recent_recorded_samples_a_tenants_records_newest_first(
    submission, gated_submission, gate_output
):
    conn = _FakeConn()
    store = _store(conn)
    ids = [f"0000000{i}-0000-0000-0000-000000000000" for i in range(3)]
    for decision_id in ids:
        store.write(_record_bundle(submission, decision_id=decision_id))
    store.write(_gated_bundle(gated_submission, gate_output, decision_id=_OTHER_ID))
    _insert_v1(conn, "99999999-9999-9999-9999-999999999999")
    base = datetime(2024, 1, 1, tzinfo=UTC)
    for offset, decision_id in enumerate(ids):
        conn.rows[decision_id].created_at = base + timedelta(minutes=offset)

    sample = store.recent_recorded("stub-reasoner", limit=2)
    assert [b.decision_id for b in sample.bundles] == [ids[2], ids[1]]
    assert sample.unloadable == 0

    since = store.recent_recorded(
        "stub-reasoner", limit=10, since=base + timedelta(minutes=1)
    )
    assert [b.decision_id for b in since.bundles] == [ids[2], ids[1]]
    assert store.recent_recorded("other", limit=10).bundles == ()


def test_recent_recorded_skips_and_counts_unloadable_rows(submission):
    conn = _FakeConn()
    store = _store(conn)
    store.write(_record_bundle(submission))
    store.write(_record_bundle(submission, decision_id=_OTHER_ID))
    broken = json.loads(conn.rows[_OTHER_ID].bundle)
    broken["gates"] = "not a list"
    conn.rows[_OTHER_ID].bundle = json.dumps(broken)
    sample = store.recent_recorded("stub-reasoner", limit=10)
    assert [b.decision_id for b in sample.bundles] == [_DECISION_ID]
    assert sample.unloadable == 1


# ---------------------------------------------------------------------------
# replay_bundle (pure)
# ---------------------------------------------------------------------------


def test_replay_bundle_reproduces_a_failed_gate(gated_submission, gate_output):
    bundle = _gated_bundle(gated_submission, gate_output, failed=True)
    assert bundle.decision_action == HOLD
    result = replay_bundle(bundle, registration=STUB_REGISTRATION)
    assert result.matched
    assert result.gates[0].status == "failed"


def test_replay_bundle_skips_skipped_gates(submission):
    result = replay_bundle(_record_bundle(submission), registration=STUB_REGISTRATION)
    assert [(g.status, g.original_action, g.replayed_action) for g in result.gates] == [
        ("skipped", None, None)
    ]
    assert result.matched


def test_replay_bundle_detects_a_competing_rule_that_changed(submission):
    bundle = _record_bundle(submission)
    tampered = bundle.model_copy(
        update={
            "other_bypass_checks": tuple(
                c.model_copy(update={"holds": not c.holds})
                for c in bundle.other_bypass_checks
            )
        }
    )
    result = replay_bundle(tampered, registration=STUB_REGISTRATION)
    assert not result.competing_rules_match
    assert not result.matched


# ---------------------------------------------------------------------------
# Registration snapshots, bulk writes, and the subject timeline (DR-31)
# ---------------------------------------------------------------------------


def _novel(gated_submission: Submission) -> Submission:
    rc = gated_submission.reasoner_context
    return gated_submission.model_copy(
        update={
            "reasoner_context": rc.model_copy(
                update={"feature_set": {**rc.feature_set, "thin_history": True}}
            )
        }
    )


def _versioned(bundle: DecisionBundle, registration: ReasonerRegistration):
    return bundle.model_copy(
        update={"registration_version": registration_version(registration)}
    )


def test_snapshots_are_written_once_per_version_and_reload_equal():
    conn = _FakeConn()
    store = _store(conn)

    version = store.snapshots.record(STUB_REGISTRATION)
    store.snapshots.record(STUB_REGISTRATION)

    assert version == registration_version(STUB_REGISTRATION)
    assert list(conn.registrations) == [("stub-reasoner", version)]
    assert conn.statements == 1  # the second record is a no-op in process
    snapshot = store.snapshots.get("stub-reasoner", version)
    assert snapshot is not None
    assert snapshot.registration == STUB_REGISTRATION
    assert store.snapshots.get("stub-reasoner", "nope") is None
    assert [s.version for s in store.snapshots.versions("stub-reasoner")] == [version]


def test_a_versioned_bundle_replays_against_its_snapshot(gated_submission, gate_output):
    # The decision was made under the registration with Tier 3; the reasoner
    # now registers without it. Replay reads the snapshot and still matches.
    conn = _FakeConn()
    store = _store(conn)
    store.snapshots.record(STUB_REGISTRATION)
    bundle = _versioned(
        _gated_bundle(_novel(gated_submission), gate_output), STUB_REGISTRATION
    )
    store.write(bundle)
    no_tier3 = STUB_REGISTRATION.model_copy(update={"novel_entity_feature": None})

    result = _store(conn, no_tier3).replay(_DECISION_ID)

    assert result.matched
    assert result.replayed_action == "HOLD"
    assert result.registration_version == registration_version(STUB_REGISTRATION)


def test_a_versioned_bundle_replays_with_the_current_registration_of_its_version(
    submission,
):
    conn = _FakeConn()  # no snapshot stored
    _store(conn).write(_versioned(_record_bundle(submission), STUB_REGISTRATION))
    assert _store(conn).replay(_DECISION_ID).matched


def test_a_missing_snapshot_of_another_version_is_refused(
    gated_submission, gate_output
):
    conn = _FakeConn()  # no snapshot stored
    bundle = _versioned(
        _gated_bundle(_novel(gated_submission), gate_output), STUB_REGISTRATION
    )
    _store(conn).write(bundle)
    no_tier3 = STUB_REGISTRATION.model_copy(update={"novel_entity_feature": None})

    with pytest.raises(RegistrationSnapshotMissingError) as err:
        _store(conn, no_tier3).replay(_DECISION_ID)
    assert err.value.version == registration_version(STUB_REGISTRATION)
    assert conn.replay_logs == []


def test_write_many_checks_every_bundle_then_sends_one_executemany(submission):
    conn = _FakeConn()
    store = _store(conn)
    bundles = [
        _record_bundle(
            submission.model_copy(update={"event_id": f"evt-{n}"}),
            decision_id=f"3333333{n}-3333-3333-3333-333333333333",
        )
        for n in range(3)
    ]

    store.write_many(bundles)
    store.write_many([])

    assert conn.executemany_calls == 1
    assert sorted(conn.rows) == sorted(b.decision_id for b in bundles)


def test_write_many_refuses_the_batch_when_one_bundle_would_not_reload(submission):
    conn = _FakeConn()
    good = _record_bundle(submission)
    bad = _record_bundle(submission, decision_id=_OTHER_ID).model_copy(
        update={"latency_breakdown": {"gate_ms": float("nan")}}
    )
    with pytest.raises(ValueError, match="does not survive the JSON round trip"):
        _store(conn).write_many([good, bad])
    assert conn.rows == {}
    assert conn.executemany_calls == 0


def test_entity_decisions_lists_a_subjects_rows_newest_first(submission):
    conn = _FakeConn()
    store = _store(conn)
    first = _record_bundle(submission)
    second = _record_bundle(
        submission.model_copy(update={"event_id": "evt-later"}), decision_id=_OTHER_ID
    ).model_copy(update={"created_at": first.created_at + timedelta(seconds=1)})
    store.write_many([first, second])
    entity_id = str(submission.entity_id)

    rows = store.entity_decisions(entity_id)
    assert [r.decision_id for r in rows] == [_OTHER_ID, _DECISION_ID]
    assert rows[0].event_id == "evt-later"
    assert rows[0].mode == "record"
    assert rows[0].entity_type == "widget"
    assert rows[0].decision_action == "ALLOW"
    assert [r.decision_id for r in store.entity_decisions(entity_id, limit=1)] == [
        _OTHER_ID
    ]
    paged = store.entity_decisions(entity_id, before=second.created_at)
    assert [r.decision_id for r in paged] == [_DECISION_ID]
    assert store.entity_decisions(entity_id, reasoner_id="other-reasoner") == []
    with pytest.raises(KeyError):
        store.entity_decisions("not-a-uuid")
    with pytest.raises(KeyError):
        store.entity_decisions(entity_id, before=second.created_at, before_id="x")
    # The cursor pair continues within an instant: the older id follows.
    tied = store.entity_decisions(
        entity_id, before=first.created_at, before_id=_OTHER_ID
    )
    assert [r.decision_id for r in tied] == [_DECISION_ID]
