"""DecisionBundle — complete replayable audit record for every decision."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from core.actions import DecisionAction
from core.gate import GateInput, GateOutput
from core.observation import Observation
from core.snippet import RetrievedSnippet


class DecisionBundle(BaseModel):
    """Complete, replayable audit record for every decision.

    Stored in PostgreSQL, indexed by ``decision_id``. Every field is logged at
    decision time — nothing is computed on read, and the row is never updated
    once written. Append-only ledger semantics.

    Domain-specific artifacts (feature snapshot, reasoner output, model
    versions for the domain reasoner) are carried in ``raw_event``: the
    Observation stores a ``FastPathRecord`` on the fast path and a fully
    rendered ``GateContext`` on all paths. Gate-invocation artifacts (input
    text, raw response, validated verdict, model/corpus versions, token
    cost) are carried in ``gate_input`` and ``gate_output`` — typed
    contracts that any gate implementation populates.

    Replay guarantee: given a ``DecisionBundle``, re-executing
    ``enforcement.resolve()`` against ``gate_output.verdict`` produces the
    same ``decision_action``. Replay does **not** re-invoke the gate. This
    holds regardless of whether the gate was an LLM, a rule engine, or any
    other implementation.

    Action vocabulary:
        * ``decision_action`` — the action this bundle produced at decision
          time. Immutable. Lives on the bundle.
        * ``resolution_action`` — for non-terminal decisions (CHALLENGE, HOLD),
          the action a resolver later produced. Lives on
          ``core.resolution.ResolutionAttempt`` rows; never on the bundle.
        * ``realized_action`` — the action ultimately taken on the entity.
          Computed from the bundle plus the resolution attempt log; never
          stored. For terminal ``decision_action`` values it equals
          ``decision_action``; otherwise it is the first terminal
          ``resolution_action`` in the attempt chain (or ``None`` if pending).

    Gate-layer field semantics by case:
        * Fast path (gate not invoked): ``gate_input is None and
          gate_output is None``.
        * Gate ran, validation succeeded: both populated; ``gate_output.verdict``
          is the typed verdict enforcement consumes.
        * Gate ran, validation failed: both populated; ``gate_output.verdict
          is None`` and (for LLM-backed gates) ``gate_output.response_text``
          carries the gate's emitted text. Enforcement routes to HOLD via
          the schema-failure tier.

    Attributes:
        decision_id: UUID of this decision record.
        created_at: When the bundle was constructed (UTC).
        raw_event: The validated domain Observation. Carries ``gate_context``
            (always) and ``fast_path_rationale`` (fast path only), which
            together contain all domain-specific provenance for replay and
            audit.
        idempotency_key: SHA-256 deduplication key computed at ingestion.
        ingestion_timestamp: When the event was acknowledged from the queue.
        queue_position: Stream sequence number, if available.
        retrieval_query: Query string sent to the retriever. Empty string on
            the fast path (retrieval not invoked).
        retrieval_results: Corpus chunks returned by the retriever. Empty
            list on the fast path.
        retrieval_path: Free-form label indicating which retrieval path was
            taken (e.g., ``"reranked"``, ``"rrf_only"``, ``"skipped"``).
        index_version: Version of the retrieval index used.
        gate_input: Typed contract capturing the gate's input artifacts at
            invocation time. ``None`` on the fast path.
        gate_output: Typed contract capturing the gate's output artifacts.
            ``None`` on the fast path. Populated even when validation
            failed — in that case ``gate_output.verdict is None``.
        decision_action: The action this decision produced. Immutable. For
            terminal actions (ALLOW, BLOCK) this is the realized action; for
            non-terminal actions (CHALLENGE, HOLD) the realized action is
            computed from the resolution attempt log.
        enforcement_rule_applied: Override rule that fired, if any.
        override_log: Ordered rule evaluation log from enforcement.
        latency_breakdown: Per-component duration in milliseconds, keyed by
            component name (e.g., ``"ingestion_ms"``, ``"gate_ms"``).
    """

    model_config = ConfigDict(strict=True, frozen=True, arbitrary_types_allowed=True)

    # Identity
    decision_id: str
    created_at: datetime

    # Input layer — domain provenance is in raw_event.gate_context
    # and raw_event.fast_path_rationale (fast path only)
    raw_event: Observation
    idempotency_key: str
    ingestion_timestamp: datetime
    queue_position: int | None

    # Retrieval layer
    retrieval_query: str
    retrieval_results: list[RetrievedSnippet]
    retrieval_path: str
    index_version: str

    # Gate layer — typed contracts, both None on fast path
    gate_input: GateInput | None
    gate_output: GateOutput | None

    # Decision layer
    decision_action: DecisionAction
    enforcement_rule_applied: str | None
    override_log: list[str]

    # Telemetry
    latency_breakdown: dict[str, float]
