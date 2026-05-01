"""Pure DecisionBundle assembly — no I/O, no infrastructure dependencies.

build_bundle() takes all pipeline outputs and assembles the complete
DecisionBundle record. It is the single place where all pipeline layer outputs
are joined into the canonical audit record.

No database writes here — assembly is pure so it can be unit-tested without
any infrastructure running.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from core.bundle import DecisionBundle

if TYPE_CHECKING:
    from app.gate.policy.gate import GateResult
    from core.enforcement import EnforcementDecision
    from core.observation import Observation
    from core.snippet import RetrievedSnippet


def build_bundle(
    *,
    decision_id: str,
    obs: Observation,
    idempotency_key: str,
    ingestion_timestamp: datetime,
    queue_position: int | None = None,
    retrieval_query: str = "",
    retrieval_results: list[RetrievedSnippet] | None = None,
    retrieval_path: str = "skipped",
    index_version: str = "unknown",
    gate_result: GateResult | None = None,
    enforcement_decision: EnforcementDecision,
    latency_breakdown: dict[str, float] | None = None,
) -> DecisionBundle:
    """Assemble a complete DecisionBundle from all pipeline layer outputs.

    This is a pure function — no I/O, no infrastructure. All inputs must be
    fully resolved before calling. The result is the canonical audit record
    for the decision.

    On the fast path, ``gate_result`` is ``None``: the gate was not invoked
    and ``gate_input`` / ``gate_output`` on the bundle are both ``None``. On
    the gate path, ``gate_result.gate_input`` and ``gate_result.gate_output``
    are passed straight through to the bundle.

    Gate-specific artifact versions (model version, corpus version, prompt
    template metadata, token cost, etc.) live inside the typed
    ``GateInput`` / ``GateOutput`` contracts on the bundle, populated by the
    gate at invocation time. The framework does not interpret them; per-gate
    docstrings enumerate what each gate writes.

    Args:
        decision_id: UUID string for this decision record.
        obs: The validated domain Observation (carries all domain provenance).
        idempotency_key: SHA-256 dedup key computed at ingestion.
        ingestion_timestamp: UTC timestamp when the event was pulled from queue.
        queue_position: Stream sequence number from the event queue, if available.
        retrieval_query: Query sent to the retriever. Empty string on the
            fast path.
        retrieval_results: Corpus chunks returned by the retriever. Defaults
            to empty list when not provided (fast path or retrieval skipped).
        retrieval_path: Free-form label indicating which retrieval path was
            taken (e.g., ``"reranked"``, ``"rrf_only"``, ``"skipped"``).
        index_version: Version of the retrieval index used.
        gate_result: Full output of the gate evaluation, or ``None`` on the
            fast path (gate was not invoked).
        enforcement_decision: Resolved decision action with override log from
            the enforcement layer.
        latency_breakdown: Per-component duration in milliseconds. Defaults to
            empty dict when not provided.

    Returns:
        Fully assembled ``DecisionBundle``.
    """
    snippets: list[RetrievedSnippet] = (
        retrieval_results if retrieval_results is not None else []
    )
    latency: dict[str, float] = (
        latency_breakdown if latency_breakdown is not None else {}
    )

    gate_input = gate_result.gate_input if gate_result is not None else None
    gate_output = gate_result.gate_output if gate_result is not None else None

    return DecisionBundle(
        decision_id=decision_id,
        created_at=datetime.now(UTC),
        raw_event=obs,
        idempotency_key=idempotency_key,
        ingestion_timestamp=ingestion_timestamp,
        queue_position=queue_position,
        retrieval_query=retrieval_query,
        retrieval_results=snippets,
        retrieval_path=retrieval_path,
        index_version=index_version,
        gate_input=gate_input,
        gate_output=gate_output,
        decision_action=enforcement_decision.decision_action,
        enforcement_rule_applied=enforcement_decision.enforcement_rule_applied,
        override_log=enforcement_decision.override_log,
        latency_breakdown=latency,
    )
