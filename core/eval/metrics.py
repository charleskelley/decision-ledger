"""Evaluation dimension metric containers and report schema."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EvalDimension(StrEnum):
    """The five evaluation dimensions run against every release candidate.

    Each dimension maps to a metric container with defined CI thresholds.
    A PR that causes any dimension to fall below its threshold is blocked
    from merge.

    Attributes:
        RETRIEVAL: Quality of the policy RAG retriever. Measures whether
            the correct policy chunks are retrieved for a given decision
            context (context precision@k, recall@k, MRR).
        FAITHFULNESS: Degree to which the LLM policy gate's reasoning is
            grounded in the retrieved documents. Penalizes hallucination
            and unsupported claims (RAGAS faithfulness, hallucination rate).
        CONSISTENCY: Stability of pipeline outputs across equivalent event
            orderings. Tests that the same scenario produces the same action
            regardless of event sequence variation (action stability rate).
        CITATION: Precision and relevance of policy citations produced by
            the policy gate, evaluated by an LLM judge on a 1-5 scale
            (citation relevance score, superficial citation rate).
        ROBUSTNESS: Resistance to adversarial inputs — prompt injection
            attempts, novel attack patterns, and schema violations
            (injection resistance rate, novel pattern accuracy).
    """

    RETRIEVAL = "retrieval"
    FAITHFULNESS = "faithfulness"
    CONSISTENCY = "consistency"
    CITATION = "citation"
    ROBUSTNESS = "robustness"


class RetrievalMetrics(BaseModel):
    """Dimension 01 — Retrieval Quality metric container.

    Evaluated against a golden query set of 30 queries, each with annotated
    relevant policy chunks. Covers direct regulatory queries, version-ambiguous
    queries, jurisdiction-scoped queries, and risk-tier-specific queries.

    CI thresholds: context_precision_at_k ≥ 0.80, context_recall_at_k ≥ 0.75,
    mean_reciprocal_rank ≥ 0.70, version_resolution_accuracy = 1.0 (zero
    tolerance), jurisdiction_filter_accuracy = 1.0 (zero tolerance).

    Args:
        k: Retrieval depth at which metrics are computed.
        context_precision_at_k: Fraction of retrieved chunks that are relevant.
        context_recall_at_k: Fraction of relevant chunks that were retrieved.
        mean_reciprocal_rank: How highly the first relevant result is ranked.
        version_resolution_accuracy: Rate at which the latest policy version is
            preferred over superseded versions.
        jurisdiction_filter_accuracy: Rate at which jurisdiction-scoped queries
            return correctly scoped results.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    k: int = Field(gt=0)
    context_precision_at_k: float = Field(ge=0.0, le=1.0)
    context_recall_at_k: float = Field(ge=0.0, le=1.0)
    mean_reciprocal_rank: float = Field(ge=0.0, le=1.0)
    version_resolution_accuracy: float = Field(ge=0.0, le=1.0)
    jurisdiction_filter_accuracy: float = Field(ge=0.0, le=1.0)


class FaithfulnessMetrics(BaseModel):
    """Dimension 02 — Generation Faithfulness metric container.

    Measures whether the LLM's rationale reflects only retrieved policy content.
    RAGAS faithfulness decomposes the rationale into atomic claims and checks
    each against the retrieved context.

    CI thresholds: ragas_faithfulness ≥ 0.85, hallucination_rate = 0.0 on the
    golden scenario set (any hallucination is a blocking failure).

    Args:
        ragas_faithfulness: Claim-level grounding score against retrieved context.
        llm_judge_grounding: Secondary LLM judge score for rationale grounding.
        citation_rationale_overlap: Text overlap between rationale claims and
            cited snippets.
        hallucination_rate: Fraction of outputs containing any claim not
            traceable to retrieved content. Zero tolerance on golden set.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    ragas_faithfulness: float = Field(ge=0.0, le=1.0)
    llm_judge_grounding: float = Field(ge=0.0, le=1.0)
    citation_rationale_overlap: float = Field(ge=0.0, le=1.0)
    hallucination_rate: float = Field(ge=0.0, le=1.0)


class ConsistencyMetrics(BaseModel):
    """Dimension 03 — Decision Consistency metric container.

    Tests 8 named scenarios x 3 event orderings = 24 consistency tests. The
    same risk signals must produce the same final action regardless of the
    order in which events are presented.

    CI thresholds: action_stability_rate = 1.0 on all 24 tests (any instability
    is a blocking failure). Rationale and confidence variance are tracked but
    not gated.

    Args:
        num_scenarios: Number of named scenarios evaluated.
        num_orderings: Number of event orderings per scenario.
        action_stability_rate: Fraction of scenario x ordering combinations where
            the final action is consistent across all orderings.
        confidence_variance: Variance in reported confidence across orderings.
            Informational — not a CI gate.
        rationale_semantic_similarity: Embedding similarity between rationale
            texts across orderings. Informational — not a CI gate.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    num_scenarios: int = Field(gt=0)
    num_orderings: int = Field(gt=0)
    action_stability_rate: float = Field(ge=0.0, le=1.0)
    confidence_variance: float = Field(ge=0.0)
    rationale_semantic_similarity: float = Field(ge=0.0, le=1.0)


class CitationMetrics(BaseModel):
    """Dimension 04 — Citation Accuracy metric container.

    Evaluates whether cited policy snippets actually support the specific claims
    they are attached to. Evaluated on a golden output set from the 8 canonical
    scenarios.

    CI thresholds: citation_relevance_score (mean) ≥ 0.80,
    superficial_citation_rate ≤ 0.05.

    Args:
        citation_relevance_score: LLM-as-judge mean score for how well each
            cited snippet supports its claim. Scale: 1-5 normalized to [0, 1].
        claim_citation_entailment: Fraction of citations where the cited text
            logically entails the attached rationale claim.
        superficial_citation_rate: Fraction of citations rated as
            present-but-irrelevant by the judge (score <= 2 on the 1-5 scale).
    """

    model_config = ConfigDict(strict=True, frozen=True)

    citation_relevance_score: float = Field(ge=0.0, le=1.0)
    claim_citation_entailment: float = Field(ge=0.0, le=1.0)
    superficial_citation_rate: float = Field(ge=0.0, le=1.0)


class RobustnessMetrics(BaseModel):
    """Dimension 05 — Adversarial Robustness metric container.

    Tests the system against injection attempts, schema violations, and event
    patterns designed to confuse the scoring and reasoning layers.

    CI thresholds: injection_resistance_rate = 1.0 (zero tolerance),
    schema_violation_handling = 1.0 (zero tolerance),
    novel_pattern_action_accuracy ≥ 0.90, fallback_behavior_correctness = 1.0
    (zero tolerance).

    Args:
        injection_resistance_rate: Fraction of injection attempts that do not
            influence the final action.
        schema_violation_handling: Fraction of malformed events correctly routed
            to HOLD.
        novel_pattern_action_accuracy: Fraction of novel adversarial patterns
            routed to the correct conservative action.
        fallback_behavior_correctness: Fraction of error scenarios that correctly
            activate the fallback path.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    injection_resistance_rate: float = Field(ge=0.0, le=1.0)
    schema_violation_handling: float = Field(ge=0.0, le=1.0)
    novel_pattern_action_accuracy: float = Field(ge=0.0, le=1.0)
    fallback_behavior_correctness: float = Field(ge=0.0, le=1.0)


class DimensionResult(BaseModel):
    """Outcome of a single evaluation dimension run.

    Args:
        dimension: Which of the five evaluation dimensions this result covers.
        passed: True if all CI gate thresholds for this dimension were met.
        metrics: Raw metric values keyed by metric name.
        threshold_violations: Human-readable descriptions of any metrics that
            failed their CI gate threshold. Empty list when ``passed`` is True.
        evaluated_at: When this dimension run completed (UTC).
        num_samples: Number of test cases evaluated.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    dimension: EvalDimension
    passed: bool
    metrics: dict[str, float]
    threshold_violations: list[str]
    evaluated_at: datetime
    num_samples: int = Field(ge=0)


class EvalReport(BaseModel):
    """Aggregated evaluation report across all five dimensions.

    Produced by ``make eval``. A report with ``overall_passed = False`` blocks
    the CI merge gate. Metric containers for each dimension are populated only
    when that dimension was included in the run.

    Args:
        run_id: UUID identifying this evaluation run.
        created_at: When the report was generated (UTC).
        overall_passed: True only when every included dimension passed its
            CI gate thresholds.
        dimensions: Per-dimension result records, in run order.
        retrieval: Dimension 01 metrics. None if dimension was skipped.
        faithfulness: Dimension 02 metrics. None if dimension was skipped.
        consistency: Dimension 03 metrics. None if dimension was skipped.
        citation: Dimension 04 metrics. None if dimension was skipped.
        robustness: Dimension 05 metrics. None if dimension was skipped.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    run_id: str
    created_at: datetime
    overall_passed: bool
    dimensions: list[DimensionResult]
    retrieval: RetrievalMetrics | None = None
    faithfulness: FaithfulnessMetrics | None = None
    consistency: ConsistencyMetrics | None = None
    citation: CitationMetrics | None = None
    robustness: RobustnessMetrics | None = None
