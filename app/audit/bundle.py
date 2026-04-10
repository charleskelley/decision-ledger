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
    from app.policy_gate.gate import GateResult
    from core.bundle import EnforcementDecision, PolicySnippet
    from core.observation import Observation


def build_bundle(
    *,
    decision_id: str,
    obs: Observation,
    idempotency_key: str,
    ingestion_timestamp: datetime,
    queue_position: int | None = None,
    retrieval_query: str = "",
    retrieval_results: list[PolicySnippet] | None = None,
    retrieval_path: str = "skipped",
    index_version: str = "unknown",
    gate_result: GateResult | None = None,
    enforcement_decision: EnforcementDecision,
    policy_corpus_version: str = "unknown",
    latency_breakdown: dict[str, float] | None = None,
) -> DecisionBundle:
    """Assemble a complete DecisionBundle from all pipeline layer outputs.

    This is a pure function — no I/O, no infrastructure. All inputs must be
    fully resolved before calling. The result is the canonical audit record
    for the decision.

    On the fast path, ``gate_result`` is ``None``: the gate was not invoked
    and all gate-layer fields default to empty/None. On the gate path,
    ``gate_result`` carries the full LLM output including the rendered prompt,
    raw response, validated output, token cost, and prompt snapshot.

    Args:
        decision_id: UUID string for this decision record.
        obs: The validated domain Observation (carries all domain provenance).
        idempotency_key: SHA-256 dedup key computed at ingestion.
        ingestion_timestamp: UTC timestamp when the event was pulled from queue.
        queue_position: Stream sequence number from the event queue, if available.
        retrieval_query: Query sent to the hybrid retriever. Empty string on
            the fast path.
        retrieval_results: Policy chunks returned by the retriever. Defaults to
            empty list when not provided (fast path or retrieval skipped).
        retrieval_path: One of ``"reranked"``, ``"rrf_only"``, or ``"skipped"``.
        index_version: Version of the policy vector index used.
        gate_result: Full output of the policy gate evaluation, or ``None``
            on the fast path (gate was not invoked).
        enforcement_decision: Final resolved enforcement action with override log.
        policy_corpus_version: Version label of the policy corpus at decision time.
        latency_breakdown: Per-component duration in milliseconds. Defaults to
            empty dict when not provided.

    Returns:
        Fully assembled ``DecisionBundle``.
    """
    snippets: list[PolicySnippet] = (
        retrieval_results if retrieval_results is not None else []
    )
    latency: dict[str, float] = (
        latency_breakdown if latency_breakdown is not None else {}
    )

    # Gate-layer fields — populated from gate_result when present.
    gr = gate_result
    rendered_prompt = gr.rendered_prompt if gr is not None else ""
    raw_llm_response = gr.raw_llm_response if gr is not None else ""
    policy_gate_output = gr.policy_gate_output if gr is not None else None
    policy_gate_snapshot = gr.prompt_snapshot if gr is not None else None
    gate_model_version = gr.model_version if gr is not None else None
    llm_token_cost = gr.token_cost if gr is not None else None

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
        gate_input_snapshot=policy_gate_snapshot,
        rendered_prompt=rendered_prompt,
        raw_llm_response=raw_llm_response,
        gate_output=policy_gate_output,
        final_action=enforcement_decision.final_action,
        enforcement_rule_applied=enforcement_decision.enforcement_rule_applied,
        override_log=enforcement_decision.override_log,
        review_packet=enforcement_decision.review_packet,
        gate_model_version=gate_model_version,
        policy_corpus_version=policy_corpus_version,
        latency_breakdown=latency,
        llm_token_cost=llm_token_cost,
    )
