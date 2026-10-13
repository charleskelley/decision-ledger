"""DecisionBundle persistence and deterministic replay (framework-owned).

BundleStore writes complete DecisionBundles to PostgreSQL and replays them
by re-executing the deterministic enforcement layer against cached
intermediate states. No gate — LLM or otherwise — is ever re-invoked during
replay, and replay reads no corpus: the stored ``GateOutput`` (verdict,
``deterministic`` flag, evidence) is all enforcement needs.

DecisionLedger is the framework-owned audit ledger; reasoners are tenants
distinguished by ``reasoner_id``. The store is reasoner-agnostic — it types
on the ``Observation`` protocol and pulls ``reasoner_id`` from
``observation.reasoner_context``. Reasoner-specific business keys (account_id,
content_id, …) live inside the JSONB bundle, never as scalar columns.

The store is gate-agnostic too (DR-28): it persists the universal
``GateInput`` / ``GateOutput`` and the gate's opaque ``evidence_snapshot`` /
``extra`` verbatim and deserializes them as the universal types. It never
imports a gate's own models.

Schema is defined canonically in ``app/audit/sql/02_tables.sql`` under the
``decisionledger`` schema and applied at startup by
``app.audit.schema.apply_canonical_schema``. Surfaced scalar columns
(``reasoner_id``, ``entity_id``, ``decision_action``) support indexed
lookups; the full bundle is stored as JSONB in ``bundle``. The
``replay_logs.diff`` column is populated only when ``is_matched = false``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from app.enforcement.resolver import resolve
from core.actions import DecisionAction
from core.bundle import DecisionBundle
from core.enforcement import EnforcementDecision  # noqa: TC001 (runtime use)
from core.exceptions import UnregisteredReasonerError
from core.gate import GateInput, GateOutput

if TYPE_CHECKING:
    from collections.abc import Mapping

    import psycopg

    from app.plugin import RawEventFactory
    from core.observation import ReasonerRegistry

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayResult:
    """Outcome of a single bundle replay.

    Args:
        decision_id: The replayed decision ID.
        original_action: Decision action recorded in the stored bundle.
        replayed_action: Decision action produced by re-running enforcement
            against the cached intermediate states.
        actions_match: True when original and replayed actions are identical.
            A mismatch signals a non-determinism bug in the enforcement layer.
        enforcement_decision: Full EnforcementDecision from the replay run,
            including the override log.
    """

    decision_id: str
    original_action: str
    replayed_action: str
    actions_match: bool
    enforcement_decision: EnforcementDecision


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _serialize_bundle(bundle: DecisionBundle) -> str:
    """Serialize a DecisionBundle to a JSON string for Postgres storage.

    Handles Pydantic models, UUIDs, datetimes, and StrEnums via a recursive
    ``model_dump(mode='json')`` strategy for each Pydantic layer plus a
    ``default=str`` fallback for any residual non-serializable values.

    Args:
        bundle: The fully assembled DecisionBundle.

    Returns:
        JSON string suitable for storage in a JSONB column.
    """
    raw_event_data = bundle.raw_event.model_dump(mode="json")  # type: ignore[attr-defined]

    data: dict[str, object] = {
        "decision_id": bundle.decision_id,
        "created_at": bundle.created_at.isoformat(),
        "raw_event": raw_event_data,
        "raw_event_type": type(bundle.raw_event).__name__,
        "idempotency_key": bundle.idempotency_key,
        "ingestion_timestamp": bundle.ingestion_timestamp.isoformat(),
        "queue_position": bundle.queue_position,
        "gate_input": (
            bundle.gate_input.model_dump(mode="json")
            if bundle.gate_input is not None
            else None
        ),
        "gate_output": (
            bundle.gate_output.model_dump(mode="json")
            if bundle.gate_output is not None
            else None
        ),
        "decision_action": bundle.decision_action.value,
        "enforcement_rule_applied": bundle.enforcement_rule_applied,
        "override_log": list(bundle.override_log),
        "latency_breakdown": dict(bundle.latency_breakdown),
    }
    return json.dumps(data, default=str)


def _deserialize_bundle(
    bundle_json: str,
    raw_event_factory: RawEventFactory,
) -> DecisionBundle:
    """Reconstruct a DecisionBundle from its stored JSON representation.

    Parses each nested Pydantic layer explicitly and uses ``strict=False``
    to coerce StrEnum values from their string representations (e.g.,
    ``"ALLOW"`` → ``DecisionAction.ALLOW``) and JSON arrays into tuples and
    frozensets. The reasoner-specific ``raw_event`` is reconstituted via the
    injected factory. The gate layer always deserializes as the universal
    ``GateInput`` / ``GateOutput``: a gate's ``evidence_snapshot`` and
    ``extra`` come back as the JSON they were stored as (DR-28).

    Args:
        bundle_json: JSON string from the ``bundle`` JSONB column.
        raw_event_factory: Reasoner-supplied callable that converts the
            JSON dict from ``bundle["raw_event"]`` back into a typed
            ``Observation``.

    Returns:
        Reconstituted ``DecisionBundle``.
    """
    data = json.loads(bundle_json)

    raw_event = raw_event_factory(data["raw_event"])

    gate_input = (
        GateInput.model_validate(data["gate_input"], strict=False)
        if data.get("gate_input") is not None
        else None
    )
    gate_output = (
        GateOutput.model_validate(data["gate_output"], strict=False)
        if data.get("gate_output") is not None
        else None
    )

    return DecisionBundle(
        decision_id=data["decision_id"],
        created_at=datetime.fromisoformat(data["created_at"]),
        raw_event=raw_event,
        idempotency_key=data["idempotency_key"],
        ingestion_timestamp=datetime.fromisoformat(data["ingestion_timestamp"]),
        queue_position=data.get("queue_position"),
        gate_input=gate_input,
        gate_output=gate_output,
        decision_action=DecisionAction(data["decision_action"]),
        enforcement_rule_applied=data.get("enforcement_rule_applied"),
        override_log=list(data["override_log"]),
        latency_breakdown=dict(data.get("latency_breakdown") or {}),
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
    comes from ``observation.reasoner_context.reasoner_id`` on write, and
    ``load()`` reads the ``reasoner_id`` column back to pick the matching
    deserializer from ``raw_event_factories`` so a stored ``raw_event`` JSON
    dict becomes that reasoner's typed Observation. Replay re-runs
    enforcement with the registration looked up in ``registry`` by the same
    ``reasoner_id`` so the enforcement profile (Tiers 3 and 5) that applied
    at decision time applies on replay. Reasoner-specific business keys
    (account_id, content_id, …) live inside the JSONB bundle payload.

    Thread safety is the caller's responsibility (psycopg connections are
    not thread-safe).

    Args:
        conn: Open psycopg3 synchronous connection.
        raw_event_factories: One deserializer per composed reasoner, keyed
            by ``reasoner_id``. Each converts a stored ``raw_event`` JSON
            dict back into that reasoner's typed Observation. Required for
            ``load()`` and ``replay()``.
        registry: Registry of known reasoners. ``replay()`` resolves the
            stored bundle's ``reasoner_id`` against it to recover the
            enforcement profile.
    """

    def __init__(
        self,
        conn: psycopg.Connection[object],
        *,
        raw_event_factories: Mapping[str, RawEventFactory],
        registry: ReasonerRegistry,
    ) -> None:
        """Initialize BundleStore with a connection, factories, and registry."""
        self._conn = conn
        self._raw_event_factories = dict(raw_event_factories)
        self._registry = registry

    def write(self, bundle: DecisionBundle) -> None:
        """Persist a DecisionBundle to the decision_bundles table.

        Uses ``ON CONFLICT DO NOTHING`` for idempotency — re-submitting a
        bundle with the same ``decision_id`` is a no-op. This supports
        at-least-once delivery semantics in the pipeline without causing
        duplicate audit records.

        ``reasoner_id`` is sourced from
        ``bundle.raw_event.reasoner_context.reasoner_id`` — the framework
        intake validator guarantees ``reasoner_context`` is populated before
        the bundle reaches the store.

        Args:
            bundle: Fully assembled bundle to persist.

        Raises:
            ValueError: If the bundle's raw_event has no reasoner_context
                (an upstream contract violation that should never occur).
        """
        bundle_json = _serialize_bundle(bundle)
        raw_event = bundle.raw_event
        if raw_event.reasoner_context is None:
            msg = (
                "Cannot persist bundle without reasoner_context — "
                "validate_observation() should have caught this upstream."
            )
            raise ValueError(msg)
        reasoner_id = raw_event.reasoner_context.reasoner_id
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO decisionledger.decision_bundles
                    (decision_id, reasoner_id, entity_id, created_at,
                     decision_action, bundle)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (decision_id) DO NOTHING
                """,
                (
                    bundle.decision_id,
                    reasoner_id,
                    str(raw_event.entity_id),
                    bundle.created_at,
                    bundle.decision_action.value,
                    bundle_json,
                ),
            )
        self._conn.commit()
        log.info(
            "audit.bundle_written",
            component="audit",
            decision_id=bundle.decision_id,
            reasoner_id=reasoner_id,
            decision_action=bundle.decision_action.value,
        )

    def load(self, decision_id: str) -> DecisionBundle:
        """Load a stored DecisionBundle by decision ID.

        The row's ``reasoner_id`` column selects which reasoner's
        ``raw_event_factory`` reconstitutes the stored ``raw_event``.

        Args:
            decision_id: The decision UUID string to look up.

        Returns:
            Reconstituted ``DecisionBundle``.

        Raises:
            KeyError: If no bundle exists for the given ``decision_id``.
            UnregisteredReasonerError: If the row's ``reasoner_id`` has no
                deserializer in this process (the reasoner that wrote the
                bundle is not composed in this deployment).
        """
        _, bundle = self._load_row(decision_id)
        return bundle

    def _load_row(self, decision_id: str) -> tuple[str, DecisionBundle]:
        """Fetch one row and return its ``reasoner_id`` with the bundle.

        Both ``load()`` and ``replay()`` go through here so that the tenant
        identity used for factory dispatch and for the registry lookup is
        the same value: the ``reasoner_id`` column stamped at write time.

        Args:
            decision_id: The decision UUID string to look up.

        Returns:
            ``(reasoner_id, bundle)`` for the stored row.

        Raises:
            KeyError: If no bundle exists for the given ``decision_id``,
                including when the id is not a well-formed UUID. The column
                is typed ``uuid``, so a malformed id would otherwise surface
                as a database error and abort the connection's transaction.
            UnregisteredReasonerError: If the row's ``reasoner_id`` has no
                deserializer in ``raw_event_factories``.
        """
        try:
            uuid.UUID(decision_id)
        except ValueError:
            raise KeyError(
                f"No bundle found for decision_id={decision_id!r} (not a UUID)"
            ) from None
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT reasoner_id, bundle FROM decisionledger.decision_bundles "
                "WHERE decision_id = %s",
                (decision_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise KeyError(f"No bundle found for decision_id={decision_id!r}")
        reasoner_id = str(row[0])  # type: ignore[index]
        factory = self._raw_event_factories.get(reasoner_id)
        if factory is None:
            raise UnregisteredReasonerError(reasoner_id)
        # psycopg3 with JSONB columns may return a dict or a str depending on
        # the connection's row factory. Normalize to str for _deserialize_bundle.
        raw = row[1]  # type: ignore[index]
        bundle_str = json.dumps(raw) if isinstance(raw, dict) else str(raw)
        return reasoner_id, _deserialize_bundle(bundle_str, factory)

    def replay(self, decision_id: str) -> ReplayResult:
        """Replay a decision by re-running enforcement against cached states.

        Loads the stored bundle and re-executes ``enforcement.resolve()``
        against its ``raw_event`` and cached ``gate_output`` (verdict,
        ``deterministic`` flag, evidence) with the registration the
        ``reasoner_id`` column resolves to in the registry. The gate is
        **never** re-invoked and no corpus is read.

        The replay result is written to ``replay_logs`` for audit purposes.

        Args:
            decision_id: The decision UUID string to replay.

        Returns:
            ``ReplayResult`` with the original and replayed actions plus match flag.

        Raises:
            KeyError: If no bundle exists for the given ``decision_id``.
            UnregisteredReasonerError: If the row's ``reasoner_id`` has no
                deserializer or is not present in the registry.
        """
        reasoner_id, bundle = self._load_row(decision_id)
        registration = self._registry.get(reasoner_id)

        replay_decision = resolve(
            bundle.raw_event,
            bundle.gate_output,
            decision_id=decision_id,
            registration=registration,
        )

        original_action = bundle.decision_action.value
        replayed_action = replay_decision.decision_action.value
        actions_match = original_action == replayed_action

        if not actions_match:
            log.error(
                "audit.replay_mismatch",
                component="audit",
                decision_id=decision_id,
                original_action=original_action,
                replayed_action=replayed_action,
            )
        else:
            log.info(
                "audit.replay_match",
                component="audit",
                decision_id=decision_id,
                action=replayed_action,
            )

        # Per canonical schema: diff is populated only when is_matched=false
        # and carries the action discrepancy plus the override log for
        # investigation. is_matched=true rows leave diff null.
        diff_payload = (
            None
            if actions_match
            else json.dumps(
                {
                    "original_action": original_action,
                    "replayed_action": replayed_action,
                    "override_log": list(replay_decision.override_log),
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
                    decision_id,
                    datetime.now(UTC),
                    actions_match,
                    diff_payload,
                ),
            )
        self._conn.commit()

        return ReplayResult(
            decision_id=decision_id,
            original_action=original_action,
            replayed_action=replayed_action,
            actions_match=actions_match,
            enforcement_decision=replay_decision,
        )
