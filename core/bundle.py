"""DecisionBundle — complete replayable audit record for every decision."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from core.actions import DecisionAction
from core.gate.output import PolicyGateOutput
from core.gate.prompt import PromptSnapshot
from core.observation.observation import Observation
from core.observation.reasoner_context import Signal


class PolicySnippet(BaseModel):
    """Retrieved policy chunk with source metadata.

    The unit of retrieval in the hybrid RAG pipeline. Each snippet carries
    enough metadata for the enforcement and audit layers to verify provenance
    without loading the full document.

    Args:
        policy_id: Identifier of the source policy document.
        title: Human-readable document title.
        version: Semantic version of the source document.
        jurisdiction: Regulatory jurisdiction of the source document.
            Plain string — domain code uses its own jurisdiction enum for
            type-safe construction (e.g., reasoner/account_takeover/).
        section_path: Hierarchical path within the document
            (e.g., "5.2.3 — Authenticator Assurance Level 2").
        text: Chunk text as retrieved (verbatim from the corpus).
        relevance_score: Score from the cross-encoder reranker or RRF fusion.
        retrieval_path: Whether cross-encoder reranking was applied.
            One of "reranked" or "rrf_only".
    """

    model_config = ConfigDict(strict=True, frozen=True)

    policy_id: str
    title: str
    version: str
    jurisdiction: str
    section_path: str
    text: str
    relevance_score: float = Field(ge=0.0)
    retrieval_path: str  # "reranked" | "rrf_only"


class TokenCost(BaseModel):
    """LLM token usage and cost breakdown for a single policy gate call.

    Args:
        prompt_tokens: Number of tokens in the prompt.
        completion_tokens: Number of tokens in the completion.
        total_tokens: Total token count (prompt + completion).
        cost_usd: Estimated cost in US dollars at the time of inference.
        model: Model identifier as returned by the API
            (e.g., ``"gpt-4o-2024-08-06"``).
    """

    model_config = ConfigDict(strict=True, frozen=True)

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)
    model: str


class ReviewPacket(BaseModel):
    """Human review context for HOLD decisions.

    Populated whenever enforcement routes to ``HOLD``. Provides sufficient
    context for a reviewer to assess the decision without accessing raw
    infrastructure state — the bundle is the authoritative source of truth.

    Args:
        decision_id: UUID of the parent Decision Bundle.
        entity_id: Framework UUID of the subject under review.
        created_at: When the HOLD was created (UTC).
        hold_reason: Plain-language reason for the hold.
        enforcement_rule: Which enforcement trigger caused the HOLD, if any.
        risk_score: Risk score at the time of decision, in [0.0, 1.0].
            Sourced from ``Observation.reasoner_context.label_value`` on the
            fast path, or from gate context signals on the gate path.
        top_signals: Top SHAP signals from the domain scorer.
        priority: Review urgency level. Higher values indicate greater urgency.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    decision_id: str
    entity_id: UUID
    created_at: datetime
    hold_reason: str
    enforcement_rule: str | None
    risk_score: float = Field(ge=0.0, le=1.0)
    top_signals: list[Signal]
    priority: int = Field(ge=0, default=0)


class EnforcementDecision(BaseModel):
    """Final action plus override log from the deterministic enforcement layer.

    The enforcement layer is purely rule-based — no LLM calls. Every execution
    path is deterministic given the same inputs. This is what makes replay work:
    re-executing enforcement against the cached ``gate_output`` from a
    bundle must produce the same ``final_action``.

    Args:
        final_action: The resolved action to take on the event.
        enforcement_rule_applied: The override rule that fired, if any. None
            when the gate output was accepted without override.
        override_log: Ordered list of rule evaluations and outcomes. Records
            every trigger check performed, including those that did not fire.
        review_packet: Human review context. Populated for all HOLD decisions;
            None for ALLOW, CHALLENGE, and BLOCK.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    final_action: DecisionAction
    enforcement_rule_applied: str | None
    override_log: list[str]
    review_packet: ReviewPacket | None


class DecisionBundle(BaseModel):
    """Complete, replayable audit record for every decision.

    Stored in PostgreSQL, indexed by ``decision_id``. Every field is logged at
    decision time — nothing is computed on read.

    Domain-specific artifacts (feature snapshot, scorer output, model versions
    for the domain scorer) are carried in ``raw_event``: the Observation stores
    a ``FastPathRecord`` on the fast path and a fully rendered ``GateContext``
    on all paths. The bundle does not duplicate this data at the top level.

    Replay guarantee: given a ``DecisionBundle``, re-executing
    ``enforcement.resolve()`` against the logged ``gate_output``
    produces the same ``final_action``. Replay does **not** re-invoke the LLM.
    The ``raw_llm_response`` and ``gate_output`` are already in the
    bundle — replay feeds the cached output back through the same deterministic
    rule evaluation.

    Args:
        decision_id: UUID of this decision record.
        created_at: When the bundle was constructed (UTC).
        raw_event: The validated domain Observation. Carries ``gate_context``
            (always) and ``fast_path_rationale`` (fast path only), which
            together contain all domain-specific provenance for replay and
            audit.
        idempotency_key: SHA-256 deduplication key computed at ingestion.
        ingestion_timestamp: When the event was acknowledged from the queue.
        queue_position: Stream sequence number, if available.
        retrieval_query: Query string sent to the hybrid retriever. Empty
            string on the fast path (retrieval not invoked).
        retrieval_results: Policy chunks returned by the retriever. Empty
            list on the fast path.
        retrieval_path: Whether reranking was applied.
            One of ``"reranked"``, ``"rrf_only"``, or ``"skipped"``.
        index_version: Version of the policy vector index used.
        gate_input_snapshot: Snapshot of the prompt template at gate
            invocation time. Combined with
            ``raw_event.gate_context.template_vars`` and
            ``retrieval_results``, an auditor can verify ``rendered_prompt``
            was correctly constructed without querying the registry. ``None``
            on the fast path (gate not invoked).
        rendered_prompt: Full prompt as sent to the LLM API. Empty string
            on the fast path.
        raw_llm_response: Raw string response from the LLM API. Empty string
            on the fast path.
        gate_output: Validated gate output. None on the fast path and when
            schema validation failed — in the latter case
            ``raw_llm_response`` has the raw output and enforcement routes
            to HOLD.
        final_action: Final resolved action.
        enforcement_rule_applied: Override rule that fired, if any.
        override_log: Ordered rule evaluation log from enforcement.
        review_packet: Human review context for HOLD decisions; None otherwise.
        gate_model_version: LLM model identifier used by the gate
            (e.g., ``"gpt-4o-2024-08-06"``). None on the fast path.
        policy_corpus_version: Version of the policy corpus at decision time.
        latency_breakdown: Per-component duration in milliseconds, keyed by
            component name (e.g., ``"ingestion_ms"``, ``"gate_ms"``).
        llm_token_cost: Token usage and cost for the LLM call. None on the
            fast path (LLM not invoked) and on LLM error.
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
    retrieval_results: list[PolicySnippet]
    retrieval_path: str  # "reranked" | "rrf_only" | "skipped"
    index_version: str

    # Gate layer
    gate_input_snapshot: PromptSnapshot | None
    rendered_prompt: str
    raw_llm_response: str
    gate_output: PolicyGateOutput | None

    # Decision layer
    final_action: DecisionAction
    enforcement_rule_applied: str | None
    override_log: list[str]
    review_packet: ReviewPacket | None

    # Artifact versions
    gate_model_version: str | None
    policy_corpus_version: str

    # Telemetry
    latency_breakdown: dict[str, float]
    llm_token_cost: TokenCost | None
