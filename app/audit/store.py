"""DecisionBundle persistence and deterministic replay (framework-owned).

BundleStore writes complete DecisionBundles to PostgreSQL and replays them by
re-executing the deterministic enforcement layer against the bundle's own
cached states. No gate — LLM or otherwise — is ever re-invoked during replay,
and replay reads no corpus and no reasoner code: a version-2 bundle stores the
reasoner's ``Submission`` (a concrete framework type, its event as opaque
JSON), every gate's universal ``GateOutput``, and the bypass rule a record
cited, as evaluated (DR-31).

DecisionLedger is the framework-owned audit ledger; reasoners are tenants
distinguished by ``reasoner_id``. The store is reasoner- and gate-agnostic:
it persists the universal contracts and each gate's opaque
``evidence_snapshot`` / ``extra`` verbatim, deserializes them as the
universal types, and never imports a reasoner's or a gate's own models.
Reasoner-specific business keys (account_id, content_id, …) live inside the
JSONB bundle, never as scalar columns.

Bundles are versioned by the ``schema_version`` column. Rows written before
the submission contract are version 1: ``lookup`` still returns them as
stored, but ``load`` and ``replay`` refuse them
(``BundleSchemaUnsupportedError``, HTTP 409 ``BUNDLE_SCHEMA_UNSUPPORTED``) —
their reasoner-typed event can no longer be reconstructed, and no production
data exists to convert.

Replay enforces against the registration the decision was made under: a
bundle records its reasoner's ``registration_version`` (DR-31), and the store
reads that version's snapshot (``app.audit.registrations``), so a decision
replays even after its registration changed or its reasoner left the
deployment. A bundle written before registrations were versioned replays
against the reasoner's current registration.

Schema is defined canonically in ``app/audit/sql/02_tables.sql`` under the
``decisionledger`` schema and applied at startup by
``app.audit.schema.apply_canonical_schema``. Surfaced scalar columns
(``reasoner_id``, ``entity_id``, ``decision_action``, ``schema_version``)
support indexed lookups — the subject's decision timeline
(``entity_decisions``) among them; the full bundle is stored as JSONB in
``bundle``. ``write_many`` inserts a batch of bundles in one round trip.
The ``replay_logs.diff`` column is populated only when ``is_matched = false``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import ValidationError

from app.audit.registrations import RegistrationSnapshots
from app.enforcement.resolver import resolve_decision, resolve_gate_output
from core.bundle import BUNDLE_SCHEMA_VERSION, DecisionBundle, GateRecord
from core.exceptions import (
    BundleSchemaUnsupportedError,
    RegistrationSnapshotMissingError,
)
from core.observation import registration_version
from core.submission import evaluate_bypass

if TYPE_CHECKING:
    from collections.abc import Sequence

    import psycopg

    from core.enforcement import EnforcementDecision
    from core.observation import ReasonerRegistration, ReasonerRegistry

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredBundle:
    """One stored row, as written, whatever its schema version.

    Args:
        decision_id: The decision.
        reasoner_id: The tenant column.
        schema_version: The bundle schema version of the row.
        payload: The stored bundle JSON (a version-1 row has the version-1
            shape; a version-2 row is a serialized ``DecisionBundle``).
    """

    decision_id: str
    reasoner_id: str
    schema_version: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class RecordedSample:
    """A sample of recorded bundles.

    Args:
        bundles: The bundles that loaded, newest first.
        unloadable: Rows that matched but no longer validate as schema
            version 2 (counted, logged, and skipped).
    """

    bundles: tuple[DecisionBundle, ...]
    unloadable: int = 0


@dataclass(frozen=True)
class DecisionSummary:
    """One decision in a subject's timeline, from the row's indexed columns.

    Args:
        decision_id: The decision.
        reasoner_id: The tenant that decided it.
        entity_id: The decision subject.
        created_at: When the bundle was written.
        decision_action: The action the decision produced.
        schema_version: The bundle schema version of the row.
        mode: ``record`` or ``gate`` (``None`` for a row older than schema
            version 2).
        event_id: The submission's event id (``None`` for an older row).
        entity_type: The subject's type (``None`` for an older row).
    """

    decision_id: str
    reasoner_id: str
    entity_id: str
    created_at: datetime
    decision_action: str
    schema_version: int
    mode: str | None
    event_id: str | None
    entity_type: str | None


@dataclass(frozen=True)
class GateReplay:
    """One gate's original and replayed enforced action.

    Args:
        gate_id: The gate.
        status: The gate's recorded status (``ran``, ``skipped``, ``failed``).
        original_action: The enforced action stored in the bundle (``None``
            for a skipped gate).
        replayed_action: The action re-running the gate-level tiers on the
            stored output produced (``None`` for a skipped gate).
    """

    gate_id: str
    status: str
    original_action: str | None
    replayed_action: str | None

    @property
    def matches(self) -> bool:
        """Whether replay reproduced the gate's enforced action."""
        return self.original_action == self.replayed_action


@dataclass(frozen=True)
class ReplayResult:
    """Outcome of a single bundle replay.

    Args:
        decision_id: The replayed decision ID.
        original_action: Decision action recorded in the stored bundle.
        replayed_action: Decision action produced by re-running enforcement
            against the cached states.
        actions_match: True when original and replayed actions are identical.
            A mismatch signals a non-determinism bug in enforcement.
        original_bypass_rule_holds: The bundle's ``bypass_rule_holds``
            (``None`` for a gated decision).
        bypass_rule_holds: The stored bypass rule re-evaluated against the
            stored evidence (``None`` for a gated decision).
        gates: Per-gate original vs replayed enforced actions, in bundle
            order.
        enforcement_decision: The replayed decision-level enforcement,
            including its log.
        competing_rules_match: Whether every other registered bypass rule
            stored with a record re-evaluates the same (``True`` for a gated
            decision).
        registration_version: The registration version replay enforced
            against: the bundle's own, or — for a bundle that records none —
            the reasoner's current one.
    """

    decision_id: str
    original_action: str
    replayed_action: str
    actions_match: bool
    original_bypass_rule_holds: bool | None
    bypass_rule_holds: bool | None
    gates: tuple[GateReplay, ...]
    enforcement_decision: EnforcementDecision
    competing_rules_match: bool = True
    registration_version: str | None = None

    @property
    def bypass_matches(self) -> bool:
        """Whether replay reproduced the bypass rule's verdict."""
        return self.original_bypass_rule_holds == self.bypass_rule_holds

    @property
    def matched(self) -> bool:
        """Replay reproduced the action, every gate's action, and the bypass checks."""
        return (
            self.actions_match
            and self.bypass_matches
            and self.competing_rules_match
            and all(gate.matches for gate in self.gates)
        )


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _serialize_bundle(bundle: DecisionBundle) -> str:
    """Serialize a DecisionBundle to the JSON stored in the JSONB column."""
    return bundle.model_dump_json()


def _deserialize_bundle(payload: dict[str, Any] | str) -> DecisionBundle:
    """Reconstruct a version-2 DecisionBundle from its stored JSON.

    Goes through JSON text on purpose: the bundle's models are strict, and
    strict validation accepts JSON's representations (ISO datetimes, UUID
    strings, arrays for tuples and sets) only from JSON input.
    """
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return DecisionBundle.model_validate_json(text)


#: One ``decision_bundles`` row; ``ON CONFLICT`` makes a rewrite a no-op.
_INSERT_BUNDLE = """
    INSERT INTO decisionledger.decision_bundles
        (decision_id, reasoner_id, entity_id, created_at,
         decision_action, schema_version, bundle)
    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
    ON CONFLICT (decision_id) DO NOTHING
"""


def _row(bundle: DecisionBundle) -> tuple[object, ...]:
    """The insert parameters of a bundle, after checking it reloads as written.

    Raises:
        ValueError: The bundle does not survive the JSON round trip.
    """
    payload = _serialize_bundle(bundle)
    try:
        reloaded: DecisionBundle | None = _deserialize_bundle(payload)
    except ValidationError:
        reloaded = None
    if reloaded != bundle:
        msg = (
            f"bundle {bundle.decision_id!r} does not survive the JSON round "
            "trip; refusing to store a decision that could not be replayed"
        )
        raise ValueError(msg)
    submission = bundle.submission
    return (
        bundle.decision_id,
        submission.reasoner_id,
        str(submission.entity_id),
        bundle.created_at,
        bundle.decision_action.value,
        BUNDLE_SCHEMA_VERSION,
        payload,
    )


# ---------------------------------------------------------------------------
# BundleStore
# ---------------------------------------------------------------------------


class BundleStore:
    """PostgreSQL persistence for DecisionBundles with deterministic replay.

    Construct once at process startup with an open psycopg connection. The
    canonical schema is applied separately by
    ``app.audit.schema.apply_canonical_schema`` before the first write.

    The store is reasoner-agnostic and multi-tenant by row: tenant identity
    comes from ``bundle.submission.reasoner_id`` on write and from the
    ``reasoner_id`` column on read. Replay reads the snapshot of the
    registration version the bundle cites for its enforcement profile
    (Tiers 3 and 5), falling back to ``registry`` for a bundle that cites
    none; everything else replay needs is in the bundle.

    The connection must be in autocommit mode (the framework's bootstrap
    opens it so), and every statement the store issues commits on its own:
    a write is one ``INSERT``, a read one ``SELECT``. The connection is
    shared — the framework's sync routes call the store from the server's
    threadpool while decisions are written from the event loop — and psycopg
    serializes statements on a connection across threads, but a transaction
    block cannot be shared that way, so the store never opens one. No read
    leaves a transaction open (an idle reader would block another process's
    startup DDL), and a failed statement leaves the connection usable.

    Args:
        conn: Open psycopg3 synchronous connection in autocommit mode.
        registry: Registry of known reasoners, for the enforcement profile
            of a bundle that cites no registration version.
        snapshots: The registration snapshot store replay reads; defaults
            to one on ``conn``.

    Raises:
        ValueError: The connection is not in autocommit mode (its writes
            would sit in a transaction no one commits).
    """

    def __init__(
        self,
        conn: psycopg.Connection[object],
        *,
        registry: ReasonerRegistry,
        snapshots: RegistrationSnapshots | None = None,
    ) -> None:
        """Initialize BundleStore with a connection and the reasoner registry."""
        if not conn.autocommit:
            msg = (
                "BundleStore needs an autocommit connection: each statement it "
                "issues commits on its own, on a connection shared across threads"
            )
            raise ValueError(msg)
        self._conn = conn
        self._registry = registry
        self._snapshots = (
            snapshots if snapshots is not None else RegistrationSnapshots(conn)
        )

    @property
    def snapshots(self) -> RegistrationSnapshots:
        """The registration snapshots replay reads (and the bootstrap writes)."""
        return self._snapshots

    def write(self, bundle: DecisionBundle) -> None:
        """Persist a DecisionBundle (schema version 2).

        Uses ``ON CONFLICT DO NOTHING`` for idempotency — re-submitting a
        bundle with the same ``decision_id`` is a no-op. This supports
        at-least-once delivery semantics in the pipeline without causing
        duplicate audit records.

        Before writing, the serialized bundle is reloaded and compared with
        the original: a bundle that would not reload identically (and so
        could never be looked up as written or replayed) is refused rather
        than stored. Intake validation makes that unreachable; the check
        keeps it that way.

        Args:
            bundle: Fully assembled bundle to persist.

        Raises:
            ValueError: The bundle does not survive the JSON round trip.
        """
        row = _row(bundle)
        with self._conn.cursor() as cur:
            cur.execute(_INSERT_BUNDLE, row)
        submission = bundle.submission
        log.info(
            "audit.bundle_written",
            component="audit",
            decision_id=bundle.decision_id,
            reasoner_id=submission.reasoner_id,
            mode=submission.mode,
            decision_action=bundle.decision_action.value,
        )

    def write_many(self, bundles: Sequence[DecisionBundle]) -> None:
        """Persist several bundles in one round trip (the batch route).

        Every bundle is checked exactly as ``write`` checks one, before any
        is sent, so a bundle that would not reload refuses the whole call and
        nothing is written. The insert is one ``executemany`` (pipelined by
        psycopg) with ``ON CONFLICT DO NOTHING`` per row, on the autocommit
        connection: each row commits on its own.

        Args:
            bundles: Fully assembled bundles to persist.

        Raises:
            ValueError: A bundle does not survive the JSON round trip.
        """
        rows = [_row(bundle) for bundle in bundles]
        if not rows:
            return
        with self._conn.cursor() as cur:
            cur.executemany(_INSERT_BUNDLE, rows)
        log.info(
            "audit.bundles_written",
            component="audit",
            count=len(rows),
            reasoner_ids=sorted({b.submission.reasoner_id for b in bundles}),
        )

    def lookup(self, decision_id: str) -> StoredBundle:
        """Return a stored row as written, whatever its schema version.

        Needs nothing from the reasoner that wrote it, so any tenant's
        bundle can be read from any deployment.

        Args:
            decision_id: The decision UUID string to look up.

        Returns:
            The stored row.

        Raises:
            KeyError: No bundle exists for ``decision_id``, including when
                the id is not a well-formed UUID (the column is typed
                ``uuid``, so a malformed id would otherwise surface as a
                database error). Any spelling Python accepts (a ``urn:uuid:``
                prefix, braces, no hyphens) is looked up in canonical form.
        """
        try:
            canonical = str(uuid.UUID(decision_id))
        except ValueError:
            raise KeyError(
                f"No bundle found for decision_id={decision_id!r} (not a UUID)"
            ) from None
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT reasoner_id, schema_version, bundle "
                "FROM decisionledger.decision_bundles WHERE decision_id = %s",
                (canonical,),
            )
            row = cur.fetchone()
        if row is None:
            raise KeyError(f"No bundle found for decision_id={decision_id!r}")
        reasoner_id, schema_version, raw = row  # type: ignore[misc]
        # psycopg3 returns JSONB as a dict by default, or as text under some
        # row factories; normalize to a dict.
        payload = raw if isinstance(raw, dict) else json.loads(str(raw))
        return StoredBundle(
            decision_id=canonical,
            reasoner_id=str(reasoner_id),
            schema_version=int(schema_version),
            payload=payload,
        )

    def load(self, decision_id: str) -> DecisionBundle:
        """Load a stored version-2 DecisionBundle by decision ID.

        Args:
            decision_id: The decision UUID string to look up.

        Returns:
            Reconstituted ``DecisionBundle``.

        Raises:
            KeyError: No bundle exists for ``decision_id``.
            BundleSchemaUnsupportedError: The row is not schema version 2.
        """
        return as_bundle(self.lookup(decision_id))

    def replay(self, decision_id: str) -> ReplayResult:
        """Replay a decision by re-running enforcement against its cached states.

        Re-runs the gate-level tiers on every gate output the bundle stores
        (a failed gate fails closed again), re-evaluates the bypass rule a
        record cited — the rule snapshotted in the bundle — against the
        stored evidence, and re-runs the decision-level rules and the
        combinator with the gates' recorded statuses. No gate is re-invoked
        and no corpus is read. The enforcement profile comes from the
        snapshot of the registration version the bundle cites
        (``registration_for``).

        The replay result is written to ``replay_logs`` for audit purposes.

        Args:
            decision_id: The decision UUID string to replay.

        Returns:
            ``ReplayResult`` with the original and replayed outcomes.

        Raises:
            KeyError: No bundle exists for ``decision_id``.
            BundleSchemaUnsupportedError: The row is not schema version 2.
            UnregisteredReasonerError: The bundle cites no registration
                snapshot and its reasoner is not registered in this
                deployment.
            RegistrationSnapshotMissingError: The bundle cites a registration
                version that has no snapshot and is not the current one.
        """
        stored = self.lookup(decision_id)
        bundle = as_bundle(stored)
        registration = self.registration_for(
            stored.reasoner_id, bundle.registration_version
        )
        result = replay_bundle(bundle, registration=registration)

        if result.matched:
            log.info(
                "audit.replay_match",
                component="audit",
                decision_id=decision_id,
                action=result.replayed_action,
            )
        else:
            log.error(
                "audit.replay_mismatch",
                component="audit",
                decision_id=decision_id,
                original_action=result.original_action,
                replayed_action=result.replayed_action,
                original_bypass_rule_holds=result.original_bypass_rule_holds,
                bypass_rule_holds=result.bypass_rule_holds,
            )

        self._log_replay(result)
        return result

    def _log_replay(self, result: ReplayResult) -> None:
        """Append the replay's outcome to ``replay_logs``."""
        # Per canonical schema: diff is populated only when is_matched=false
        # and carries every discrepancy plus the replayed decision log for
        # investigation. is_matched=true rows leave diff null.
        diff_payload = (
            None
            if result.matched
            else json.dumps(
                {
                    "original_action": result.original_action,
                    "replayed_action": result.replayed_action,
                    "original_bypass_rule_holds": result.original_bypass_rule_holds,
                    "bypass_rule_holds": result.bypass_rule_holds,
                    "gates": [
                        {
                            "gate_id": g.gate_id,
                            "original_action": g.original_action,
                            "replayed_action": g.replayed_action,
                        }
                        for g in result.gates
                        if not g.matches
                    ],
                    "override_log": list(result.enforcement_decision.override_log),
                },
                default=str,
            )
        )
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO decisionledger.replay_logs
                    (decision_id, replayed_at, is_matched, diff)
                VALUES (%s, %s, %s, %s::jsonb)
                """,
                (
                    result.decision_id,
                    datetime.now(UTC),
                    result.matched,
                    diff_payload,
                ),
            )

    def registration_for(
        self, reasoner_id: str, version: str | None
    ) -> ReasonerRegistration:
        """Return the registration a decision was made under.

        Args:
            reasoner_id: The bundle's reasoner.
            version: The ``registration_version`` the bundle cites, or
                ``None`` for a bundle written before registrations were
                versioned.

        Returns:
            The snapshot of ``version`` when one is stored; otherwise the
            reasoner's current registration, provided it is that version (or
            the bundle cites none).

        Raises:
            UnregisteredReasonerError: The bundle cites no version and the
                reasoner is not registered in this deployment.
            RegistrationSnapshotMissingError: ``version`` has no snapshot and
                the reasoner is not registered here under that version.
        """
        if version is None:
            return self._registry.get(reasoner_id)
        snapshot = self._snapshots.get(reasoner_id, version)
        if snapshot is not None:
            return snapshot.registration
        if self._registry.is_registered(reasoner_id):
            current = self._registry.get(reasoner_id)
            if registration_version(current) == version:
                return current
        raise RegistrationSnapshotMissingError(reasoner_id, version)

    def entity_decisions(
        self,
        entity_id: str,
        *,
        reasoner_id: str | None = None,
        limit: int = 50,
        before: datetime | None = None,
        before_id: str | None = None,
    ) -> list[DecisionSummary]:
        """Return a subject's decisions, newest first (its ledger timeline).

        Reads the indexed scalar columns plus three submission fields, so it
        serves rows of any schema version and needs no reasoner. Rows are
        ordered by ``(created_at, decision_id)``, newest first, and the page
        cursor is that same pair, so decisions written in the same instant
        are neither skipped nor repeated across pages.

        Args:
            entity_id: The decision subject (a UUID string).
            reasoner_id: Only this tenant's decisions, when given.
            limit: Maximum number of decisions.
            before: The ``created_at`` of the last decision a caller has
                seen: only decisions after it in the timeline's order.
            before_id: The ``decision_id`` of that decision. Without it,
                ``before`` alone excludes every decision written at that
                instant.

        Returns:
            Up to ``limit`` summaries, newest first.

        Raises:
            KeyError: ``entity_id`` or ``before_id`` is not a well-formed
                UUID.
        """
        try:
            canonical = str(uuid.UUID(entity_id))
        except ValueError:
            raise KeyError(f"entity_id {entity_id!r} is not a UUID") from None
        try:
            cursor_id = str(uuid.UUID(before_id)) if before_id is not None else None
        except ValueError:
            raise KeyError(f"before_id {before_id!r} is not a UUID") from None
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT decision_id, reasoner_id, entity_id, created_at,
                       decision_action, schema_version,
                       bundle -> 'submission' ->> 'mode',
                       bundle -> 'submission' ->> 'event_id',
                       bundle -> 'submission' ->> 'entity_type'
                FROM decisionledger.decision_bundles
                WHERE entity_id = %s
                  AND (%s::text IS NULL OR reasoner_id = %s::text)
                  AND (
                      %s::timestamptz IS NULL
                      OR created_at < %s::timestamptz
                      OR (
                          created_at = %s::timestamptz
                          AND %s::uuid IS NOT NULL
                          AND decision_id < %s::uuid
                      )
                  )
                ORDER BY created_at DESC, decision_id DESC
                LIMIT %s
                """,
                (
                    canonical,
                    reasoner_id,
                    reasoner_id,
                    before,
                    before,
                    before,
                    cursor_id,
                    cursor_id,
                    limit,
                ),
            )
            rows = cur.fetchall()
        return [
            DecisionSummary(
                decision_id=str(decision_id),
                reasoner_id=str(row_reasoner),
                entity_id=str(row_entity),
                created_at=created_at,
                decision_action=str(action),
                schema_version=int(schema_version),
                mode=mode,
                event_id=event_id,
                entity_type=entity_type,
            )
            for (
                decision_id,
                row_reasoner,
                row_entity,
                created_at,
                action,
                schema_version,
                mode,
                event_id,
                entity_type,
            ) in rows  # type: ignore[misc]
        ]

    def recent_recorded(
        self,
        reasoner_id: str,
        *,
        limit: int,
        since: datetime | None = None,
    ) -> RecordedSample:
        """Return a reasoner's most recent recorded (``mode=record``) bundles.

        The sample the shadow evaluation dimension evaluates the reasoner's
        advisory gates on. Version-1 rows are never returned, and a row that
        no longer loads is counted and skipped rather than failing the
        whole sample.

        Args:
            reasoner_id: The tenant.
            limit: Maximum number of bundles, newest first.
            since: Only bundles created at or after this instant (an eval
                run passes its own start, so it samples what it recorded).

        Returns:
            Up to ``limit`` version-2 bundles, newest first, and how many
            rows were skipped as unloadable.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT decision_id, bundle FROM decisionledger.decision_bundles
                WHERE reasoner_id = %s
                  AND schema_version = %s
                  AND bundle -> 'submission' ->> 'mode' = 'record'
                  AND (%s::timestamptz IS NULL OR created_at >= %s::timestamptz)
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (reasoner_id, BUNDLE_SCHEMA_VERSION, since, since, limit),
            )
            rows = cur.fetchall()
        bundles: list[DecisionBundle] = []
        unloadable = 0
        for decision_id, payload in rows:  # type: ignore[misc]
            try:
                bundles.append(_deserialize_bundle(payload))
            except ValidationError as err:
                unloadable += 1
                log.warning(
                    "audit.bundle_unloadable",
                    component="audit",
                    decision_id=str(decision_id),
                    error=str(err),
                )
        return RecordedSample(bundles=tuple(bundles), unloadable=unloadable)


def replay_bundle(
    bundle: DecisionBundle, *, registration: ReasonerRegistration
) -> ReplayResult:
    """Re-run enforcement on a bundle's cached states. Pure; no I/O.

    Args:
        bundle: The stored bundle.
        registration: Its reasoner's registration (enforcement profile).

    Returns:
        The replay outcome (nothing is written).
    """
    decision_id = bundle.decision_id
    replayed_records: list[GateRecord] = []
    gate_replays: list[GateReplay] = []
    for record in bundle.gates:
        if record.enforcement is None:  # skipped: nothing was enforced
            replayed_records.append(record)
            gate_replays.append(
                GateReplay(
                    gate_id=record.gate_id,
                    status=record.status,
                    original_action=None,
                    replayed_action=None,
                )
            )
            continue
        enforcement = resolve_gate_output(
            record.output,
            registration=registration,
            decision_id=decision_id,
            failure=record.reason,
        )
        replayed_records.append(record.model_copy(update={"enforcement": enforcement}))
        gate_replays.append(
            GateReplay(
                gate_id=record.gate_id,
                status=record.status,
                original_action=record.enforcement.decision_action.value,
                replayed_action=enforcement.decision_action.value,
            )
        )

    evidence = bundle.submission.reasoner_context
    stored_check = bundle.bypass_check
    replayed_check = (
        evaluate_bypass(stored_check.rule, evidence)
        if stored_check is not None
        else None
    )
    replayed_competing = tuple(
        evaluate_bypass(check.rule, evidence) for check in bundle.other_bypass_checks
    )
    decision = resolve_decision(
        submission=bundle.submission,
        records=replayed_records,
        registration=registration,
        bypass_check=replayed_check,
        competing_checks=replayed_competing,
        decision_id=decision_id,
    )
    original_action = bundle.decision_action.value
    replayed_action = decision.decision_action.value
    return ReplayResult(
        decision_id=decision_id,
        original_action=original_action,
        replayed_action=replayed_action,
        actions_match=original_action == replayed_action,
        original_bypass_rule_holds=bundle.bypass_rule_holds,
        bypass_rule_holds=replayed_check.holds if replayed_check is not None else None,
        gates=tuple(gate_replays),
        enforcement_decision=decision,
        competing_rules_match=tuple(c.holds for c in replayed_competing)
        == tuple(c.holds for c in bundle.other_bypass_checks),
        registration_version=registration_version(registration),
    )


def as_bundle(stored: StoredBundle) -> DecisionBundle:
    """Return a stored row as a typed version-2 bundle.

    Args:
        stored: A row from ``BundleStore.lookup``.

    Returns:
        The validated ``DecisionBundle``.

    Raises:
        BundleSchemaUnsupportedError: The row is not schema version 2.
    """
    if stored.schema_version != BUNDLE_SCHEMA_VERSION:
        raise BundleSchemaUnsupportedError(
            stored.decision_id,
            stored.schema_version,
            supported=BUNDLE_SCHEMA_VERSION,
        )
    return _deserialize_bundle(stored.payload)
