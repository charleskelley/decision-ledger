"""DecisionBundle persistence and deterministic replay (framework-owned).

BundleStore writes complete DecisionBundles to PostgreSQL and replays them
by re-executing the deterministic enforcement layer against cached
intermediate states. The LLM is never re-invoked during replay.

DecisionLedger is the framework-owned audit ledger; reasoners are tenants
distinguished by ``reasoner_id``. The store is reasoner-agnostic — it types
on the ``Observation`` protocol and pulls ``reasoner_id`` from
``observation.reasoner_context``. Reasoner-specific business keys (account_id,
content_id, …) live inside the JSONB bundle, never as scalar columns.

Schema is defined canonically in ``infra/postgres/02_tables.sql`` under the
``decisionledger`` schema. ``ensure_schema`` here mirrors that DDL so the
store can initialize a fresh dev database without depending on the init
scripts having run. Surfaced scalar columns (``reasoner_id``, ``entity_id``,
``decision_action``) support indexed lookups; the full bundle is stored as
JSONB in ``bundle``. The ``replay_logs.diff`` column is populated only when
``is_matched = false``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from app.enforcement.resolver import resolve
from core.bundle import DecisionBundle
from core.enforcement import EnforcementDecision  # noqa: TC001 (runtime use)
from core.gate.policy import PolicyGateInput, PolicyGateOutput
from core.snippet import RetrievedSnippet

if TYPE_CHECKING:
    import psycopg

log = structlog.get_logger(__name__)

#: Deserializer signature for a reasoner's raw event payload. The store calls
#: this on the JSON dict from ``bundle["raw_event"]`` to reconstitute a typed
#: Observation for replay. Provided by the deployment composer at construction
#: time so the store stays reasoner-agnostic. The return is typed loosely as
#: ``Any`` because Pydantic ``computed_field`` properties make
#: concrete event classes structurally — but not nominally — compatible with
#: the ``Observation`` Protocol; downstream code duck-types on the protocol.
RawEventFactory = Callable[[dict[str, Any]], Any]

# ---------------------------------------------------------------------------
# DDL — mirrors infra/postgres/02_tables.sql
# ---------------------------------------------------------------------------

_CREATE_SCHEMA = "CREATE SCHEMA IF NOT EXISTS decisionledger;"

_CREATE_BUNDLES_TABLE = """
CREATE TABLE IF NOT EXISTS decisionledger.decision_bundles (
    decision_id     UUID PRIMARY KEY,
    reasoner_id     TEXT NOT NULL,
    entity_id       UUID NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    decision_action TEXT NOT NULL,
    bundle          JSONB NOT NULL
);
"""

_CREATE_REPLAY_LOGS_TABLE = """
CREATE TABLE IF NOT EXISTS decisionledger.replay_logs (
    replay_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id UUID NOT NULL
        REFERENCES decisionledger.decision_bundles(decision_id),
    replayed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_matched  BOOLEAN NOT NULL,
    diff        JSONB
);
"""


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
        "retrieval_query": bundle.retrieval_query,
        "retrieval_results": [
            s.model_dump(mode="json") for s in bundle.retrieval_results
        ],
        "retrieval_path": bundle.retrieval_path,
        "index_version": bundle.index_version,
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
    ``"ALLOW"`` → ``DecisionAction.ALLOW``). The reasoner-specific
    ``raw_event`` is reconstituted via the injected factory.

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

    retrieval_results = [
        RetrievedSnippet.model_validate(r, strict=False)
        for r in data["retrieval_results"]
    ]

    # Discriminated-union deserialization: pick concrete subclass by gate_id.
    # MVP closes over a single variant (PolicyGateInput / PolicyGateOutput);
    # a second gate kind would be added here as a Union arm.
    gate_input = (
        PolicyGateInput.model_validate(data["gate_input"], strict=False)
        if data.get("gate_input") is not None
        else None
    )

    gate_output = (
        PolicyGateOutput.model_validate(data["gate_output"], strict=False)
        if data.get("gate_output") is not None
        else None
    )

    from core.actions import DecisionAction

    return DecisionBundle(
        decision_id=data["decision_id"],
        created_at=datetime.fromisoformat(data["created_at"]),
        raw_event=raw_event,
        idempotency_key=data["idempotency_key"],
        ingestion_timestamp=datetime.fromisoformat(data["ingestion_timestamp"]),
        queue_position=data.get("queue_position"),
        retrieval_query=data["retrieval_query"],
        retrieval_results=retrieval_results,
        retrieval_path=data["retrieval_path"],
        index_version=data["index_version"],
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
    ``ensure_schema()`` method creates the required tables if they do not
    exist — call it once during startup before the first write.

    The store is reasoner-agnostic: tenant identity comes from
    ``observation.reasoner_context.reasoner_id`` on write, and replay
    deserializes the stored ``raw_event`` JSON via the ``raw_event_factory``
    callable supplied at construction time. Reasoner-specific business keys
    (account_id, content_id, …) live inside the JSONB bundle payload.

    Thread safety is the caller's responsibility (psycopg connections are
    not thread-safe).

    Args:
        conn: Open psycopg3 synchronous connection.
        raw_event_factory: Reasoner-supplied deserializer that converts a
            stored ``raw_event`` JSON dict back into a typed Observation.
            Required for ``load()`` and ``replay()``.
    """

    def __init__(
        self,
        conn: psycopg.Connection[object],
        *,
        raw_event_factory: RawEventFactory,
    ) -> None:
        """Initialize BundleStore with a psycopg connection and event factory."""
        self._conn = conn
        self._raw_event_factory = raw_event_factory

    def ensure_schema(self) -> None:
        """Create the schema and tables if they do not exist.

        Idempotent — safe to call on every startup.
        """
        with self._conn.cursor() as cur:
            cur.execute(_CREATE_SCHEMA)
            cur.execute(_CREATE_BUNDLES_TABLE)
            cur.execute(_CREATE_REPLAY_LOGS_TABLE)
        self._conn.commit()

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

        Args:
            decision_id: The decision UUID string to look up.

        Returns:
            Reconstituted ``DecisionBundle``.

        Raises:
            KeyError: If no bundle exists for the given ``decision_id``.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT bundle FROM decisionledger.decision_bundles "
                "WHERE decision_id = %s",
                (decision_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise KeyError(f"No bundle found for decision_id={decision_id!r}")
        # psycopg3 with JSONB columns may return a dict or a str depending on
        # the connection's row factory. Normalize to str for _deserialize_bundle.
        raw = row[0]  # type: ignore[index]
        bundle_str = json.dumps(raw) if isinstance(raw, dict) else str(raw)
        return _deserialize_bundle(bundle_str, self._raw_event_factory)

    def replay(self, decision_id: str) -> ReplayResult:
        """Replay a decision by re-running enforcement against cached states.

        Loads the stored bundle, extracts the cached ``policy_gate_output``
        and ``retrieval_results``, and re-executes ``enforcement.resolve()``
        deterministically. The LLM is **never** re-invoked.

        The replay result is written to ``replay_logs`` for audit purposes.

        Args:
            decision_id: The decision UUID string to replay.

        Returns:
            ``ReplayResult`` with the original and replayed actions plus match flag.

        Raises:
            KeyError: If no bundle exists for the given ``decision_id``.
        """
        bundle = self.load(decision_id)

        verdict = bundle.gate_output.verdict if bundle.gate_output is not None else None
        replay_decision = resolve(
            bundle.raw_event,
            verdict,
            snippets=list(bundle.retrieval_results),
            decision_id=decision_id,
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
