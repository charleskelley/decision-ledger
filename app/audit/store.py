"""DecisionBundle persistence and deterministic replay.

BundleStore writes complete DecisionBundles to PostgreSQL and replays them
by re-executing the deterministic enforcement layer against cached intermediate
states. The LLM is never re-invoked during replay.

Schema (created automatically on first write)::

    decision_bundles(
        decision_id    TEXT PRIMARY KEY,
        created_at     TIMESTAMPTZ NOT NULL,
        idempotency_key TEXT NOT NULL,
        bundle_json    JSONB NOT NULL
    )

    replay_logs(
        replay_id      TEXT PRIMARY KEY,  -- UUID
        decision_id    TEXT NOT NULL REFERENCES decision_bundles,
        replayed_at    TIMESTAMPTZ NOT NULL,
        original_action TEXT NOT NULL,
        replayed_action TEXT NOT NULL,
        actions_match  BOOLEAN NOT NULL,
        override_log   JSONB NOT NULL
    )
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from app.enforcement.resolver import resolve
from core.bundle import DecisionBundle, EnforcementDecision, PolicySnippet
from core.gate import PolicyGateOutput, PromptSnapshot
from reasoner.account_takeover.events import LoginEvent

if TYPE_CHECKING:
    import psycopg

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_CREATE_BUNDLES_TABLE = """
CREATE TABLE IF NOT EXISTS decision_bundles (
    decision_id     TEXT PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL,
    idempotency_key TEXT NOT NULL,
    bundle_json     JSONB NOT NULL
);
"""

_CREATE_REPLAY_LOGS_TABLE = """
CREATE TABLE IF NOT EXISTS replay_logs (
    replay_id       TEXT PRIMARY KEY,
    decision_id     TEXT NOT NULL REFERENCES decision_bundles(decision_id),
    replayed_at     TIMESTAMPTZ NOT NULL,
    original_action TEXT NOT NULL,
    replayed_action TEXT NOT NULL,
    actions_match   BOOLEAN NOT NULL,
    override_log    JSONB NOT NULL
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
        original_action: Final action recorded in the stored bundle.
        replayed_action: Final action produced by re-running enforcement against
            the cached intermediate states.
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
        "gate_input_snapshot": (
            bundle.gate_input_snapshot.model_dump(mode="json")
            if bundle.gate_input_snapshot is not None
            else None
        ),
        "rendered_prompt": bundle.rendered_prompt,
        "raw_llm_response": bundle.raw_llm_response,
        "gate_output": (
            bundle.gate_output.model_dump(mode="json")
            if bundle.gate_output is not None
            else None
        ),
        "final_action": bundle.final_action.value,
        "enforcement_rule_applied": bundle.enforcement_rule_applied,
        "override_log": list(bundle.override_log),
        "review_packet": (
            bundle.review_packet.model_dump(mode="json")
            if bundle.review_packet is not None
            else None
        ),
        "gate_model_version": bundle.gate_model_version,
        "policy_corpus_version": bundle.policy_corpus_version,
        "latency_breakdown": dict(bundle.latency_breakdown),
        "llm_token_cost": (
            bundle.llm_token_cost.model_dump(mode="json")
            if bundle.llm_token_cost is not None
            else None
        ),
    }
    return json.dumps(data, default=str)


def _deserialize_bundle(bundle_json: str) -> DecisionBundle:
    """Reconstruct a DecisionBundle from its stored JSON representation.

    Parses each nested Pydantic layer explicitly and uses ``strict=False``
    to coerce StrEnum values from their string representations (e.g.,
    ``"ALLOW"`` → ``DecisionAction.ALLOW``).

    Args:
        bundle_json: JSON string from the ``bundle_json`` column.

    Returns:
        Reconstituted ``DecisionBundle``.
    """
    data = json.loads(bundle_json)

    raw_event = LoginEvent.model_validate(data["raw_event"], strict=False)

    retrieval_results = [
        PolicySnippet.model_validate(r, strict=False) for r in data["retrieval_results"]
    ]

    gate_input_snapshot = (
        PromptSnapshot.model_validate(data["gate_input_snapshot"], strict=False)
        if data.get("gate_input_snapshot") is not None
        else None
    )

    gate_output = (
        PolicyGateOutput.model_validate(data["gate_output"], strict=False)
        if data.get("gate_output") is not None
        else None
    )

    review_packet_data = data.get("review_packet")
    review_packet = None
    if review_packet_data is not None:
        from core.bundle import ReviewPacket

        review_packet = ReviewPacket.model_validate(review_packet_data, strict=False)

    llm_token_cost_data = data.get("llm_token_cost")
    llm_token_cost = None
    if llm_token_cost_data is not None:
        from core.bundle import TokenCost

        llm_token_cost = TokenCost.model_validate(llm_token_cost_data, strict=False)

    from core.actions import DecisionAction

    return DecisionBundle(
        decision_id=data["decision_id"],
        created_at=datetime.fromisoformat(data["created_at"]),
        raw_event=raw_event,  # type: ignore[arg-type]
        idempotency_key=data["idempotency_key"],
        ingestion_timestamp=datetime.fromisoformat(data["ingestion_timestamp"]),
        queue_position=data.get("queue_position"),
        retrieval_query=data["retrieval_query"],
        retrieval_results=retrieval_results,
        retrieval_path=data["retrieval_path"],
        index_version=data["index_version"],
        gate_input_snapshot=gate_input_snapshot,
        rendered_prompt=data["rendered_prompt"],
        raw_llm_response=data["raw_llm_response"],
        gate_output=gate_output,
        final_action=DecisionAction(data["final_action"]),
        enforcement_rule_applied=data.get("enforcement_rule_applied"),
        override_log=list(data["override_log"]),
        review_packet=review_packet,
        gate_model_version=data.get("gate_model_version"),
        policy_corpus_version=data["policy_corpus_version"],
        latency_breakdown=dict(data.get("latency_breakdown") or {}),
        llm_token_cost=llm_token_cost,
    )


# ---------------------------------------------------------------------------
# BundleStore
# ---------------------------------------------------------------------------


class BundleStore:
    """PostgreSQL persistence for DecisionBundles with deterministic replay.

    Construct once at process startup with an open psycopg connection. The
    ``ensure_schema()`` method creates the required tables if they do not
    exist — call it once during startup before the first write.

    The store is stateless beyond the connection. Thread safety is the
    caller's responsibility (psycopg connections are not thread-safe).

    Args:
        conn: Open psycopg3 synchronous connection.
    """

    def __init__(self, conn: psycopg.Connection[object]) -> None:
        """Initialize BundleStore with a psycopg connection."""
        self._conn = conn

    def ensure_schema(self) -> None:
        """Create decision_bundles and replay_logs tables if they do not exist.

        Idempotent — safe to call on every startup.
        """
        with self._conn.cursor() as cur:
            cur.execute(_CREATE_BUNDLES_TABLE)
            cur.execute(_CREATE_REPLAY_LOGS_TABLE)
        self._conn.commit()

    def write(self, bundle: DecisionBundle) -> None:
        """Persist a DecisionBundle to the decision_bundles table.

        Uses ``ON CONFLICT DO NOTHING`` for idempotency — re-submitting a
        bundle with the same ``decision_id`` is a no-op. This supports
        at-least-once delivery semantics in the pipeline without causing
        duplicate audit records.

        Args:
            bundle: Fully assembled bundle to persist.
        """
        bundle_json = _serialize_bundle(bundle)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO decision_bundles
                    (decision_id, created_at, idempotency_key, bundle_json)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (decision_id) DO NOTHING
                """,
                (
                    bundle.decision_id,
                    bundle.created_at,
                    bundle.idempotency_key,
                    bundle_json,
                ),
            )
        self._conn.commit()
        log.info(
            "audit.bundle_written",
            component="audit",
            decision_id=bundle.decision_id,
            final_action=bundle.final_action.value,
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
                "SELECT bundle_json FROM decision_bundles WHERE decision_id = %s",
                (decision_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise KeyError(f"No bundle found for decision_id={decision_id!r}")
        # psycopg3 with JSONB columns may return a dict or a str depending on
        # the connection's row factory. Normalize to str for _deserialize_bundle.
        raw = row[0]  # type: ignore[index]
        bundle_str = json.dumps(raw) if isinstance(raw, dict) else str(raw)
        return _deserialize_bundle(bundle_str)

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

        replay_decision = resolve(
            bundle.raw_event,
            bundle.policy_gate_output,
            snippets=list(bundle.retrieval_results),
            decision_id=decision_id,
        )

        original_action = bundle.final_action.value
        replayed_action = replay_decision.final_action.value
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

        replay_id = str(uuid.uuid4())
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO replay_logs
                    (replay_id, decision_id, replayed_at, original_action,
                     replayed_action, actions_match, override_log)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    replay_id,
                    decision_id,
                    datetime.now(UTC),
                    original_action,
                    replayed_action,
                    actions_match,
                    json.dumps(replay_decision.override_log),
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
