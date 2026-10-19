"""Evaluation dimension metric containers and report schema."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EvalDimension(StrEnum):
    """The evaluation dimensions an ``EvalReport`` can carry.

    Each dimension has one metric container below. The framework's harness
    drives a reasoner's own pipeline over the datasets the reasoner ships
    (CONSISTENCY, ROBUSTNESS), then shadows the decisions recorded in that
    run (SHADOW). A gate's own harness runs the dimensions that measure that
    gate (RETRIEVAL, FAITHFULNESS, CITATION) over the datasets each reasoner
    ships for it. The harness that runs a dimension owns its thresholds, and
    the dimension fails when a metric misses one or its dataset is missing.
    Evaluation runs on demand; no merge check runs it.

    Attributes:
        RETRIEVAL: Quality of a retrieving gate's retriever. Measures whether
            the documents annotated relevant to each golden query come back,
            the expected one at its current version, within the query's
            jurisdiction filter (context precision@k, recall@k, MRR).
        FAITHFULNESS: Degree to which a gate's generated rationale is
            grounded in the evidence it retrieved. Penalizes hallucination
            and unsupported claims (claim-level faithfulness, judge
            grounding, hallucination rate).
        CONSISTENCY: Stability of pipeline outputs across equivalent event
            orderings. Tests that the same scenario produces the same action
            regardless of event sequence variation (action stability rate).
        CITATION: Precision and relevance of the citations a gate produces,
            evaluated by LLM judges: support on a 1-5 scale, and entailment
            (citation relevance score, superficial citation rate).
        ROBUSTNESS: Resistance to adversarial inputs and gate failures:
            prompt injection attempts, gate outputs that fail schema
            validation, novel attack patterns, and forced fallbacks
            (injection resistance rate, novel pattern accuracy).
        SHADOW: How recorded decisions compare with what the ledger would
            have decided had the reasoner asked for a gate: a sample of
            recorded bundles is re-decided offline with the reasoner's
            advisory gates (agreement, escalation, relaxation rates).
            Report-only: it applies no threshold.
    """

    RETRIEVAL = "retrieval"
    FAITHFULNESS = "faithfulness"
    CONSISTENCY = "consistency"
    CITATION = "citation"
    ROBUSTNESS = "robustness"
    SHADOW = "shadow"


class RetrievalMetrics(BaseModel):
    """Dimension 01 — Retrieval Quality metric container.

    Computed over a reasoner's golden queries, each annotated with its
    relevant documents, the document and version expected near the top, and
    any jurisdiction filter. The thresholds belong to the harness of the
    gate that runs this dimension.

    Attributes:
        k: Retrieval depth: results requested per query, and the cutoff for
            precision and recall.
        context_precision_at_k: Mean over queries of the fraction of the top
            k results whose document is annotated relevant (missing results
            count against it).
        context_recall_at_k: Mean over queries of the fraction of annotated
            relevant documents found in the top k.
        mean_reciprocal_rank: How highly the first relevant result is ranked
            (mean over queries of 1 / its rank; 0 when none is retrieved).
        version_resolution_accuracy: Fraction of queries whose expected
            document appears, at its expected version, among the top
            results. Catches a superseded version displacing the current one.
        jurisdiction_filter_accuracy: Fraction of queries whose results all
            lie within the query's jurisdiction filter (a query without one
            counts as correct).
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

    Measures whether a gate's rationale reflects only its retrieved evidence.
    Computed over a reasoner's golden cases, each a rationale with the
    contexts it had and the snippets it cited. The claim-level faithfulness
    score decomposes the rationale into atomic claims and checks each against
    the contexts (the gate's eval names its scorer). The thresholds belong to
    the harness of the gate that runs this dimension.

    Attributes:
        ragas_faithfulness: Claim-level grounding score against retrieved
            context, averaged over cases. The key keeps its original name so
            committed reports still load.
        llm_judge_grounding: Secondary LLM judge score for rationale
            grounding, averaged over cases.
        citation_rationale_overlap: Word-level Jaccard similarity between the
            rationale and the text of the snippets it cites, averaged over
            cases.
        hallucination_rate: Fraction of cases whose judge grounding score
            falls below the harness's hallucination boundary.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    ragas_faithfulness: float = Field(ge=0.0, le=1.0)
    llm_judge_grounding: float = Field(ge=0.0, le=1.0)
    citation_rationale_overlap: float = Field(ge=0.0, le=1.0)
    hallucination_rate: float = Field(ge=0.0, le=1.0)


class ConsistencyMetrics(BaseModel):
    """Dimension 03 — Decision Consistency metric container.

    Each scenario in a reasoner's dataset presents the same events in at
    least two orderings; the last event of each ordering is the trigger. The
    same events must produce the same final action for the trigger regardless
    of the order in which they are presented.

    The framework's harness requires action_stability_rate = 1.0 (zero
    tolerance: one unstable scenario fails the dimension). Confidence
    variance and rationale similarity are recorded but not gated.

    Attributes:
        num_scenarios: Number of scenarios evaluated.
        num_orderings: Number of event orderings per scenario (the fewest,
            when scenarios differ).
        action_stability_rate: Fraction of scenarios whose orderings all
            reached the same final action.
        confidence_variance: Mean over scenarios of the variance, across
            orderings, of the gate confidence each run reported (runs without
            one are left out). Informational, never gated.
        rationale_semantic_similarity: Reserved for the similarity of
            rationale texts across orderings. Not computed yet: the
            framework's harness reports 1.0. Informational, never gated.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    num_scenarios: int = Field(gt=0)
    num_orderings: int = Field(gt=0)
    action_stability_rate: float = Field(ge=0.0, le=1.0)
    confidence_variance: float = Field(ge=0.0)
    rationale_semantic_similarity: float = Field(ge=0.0, le=1.0)


class CitationMetrics(BaseModel):
    """Dimension 04 — Citation Accuracy metric container.

    Evaluates whether cited snippets actually support the specific claims
    they are attached to. Computed over a reasoner's golden (claim, cited
    snippet) cases, each scored by two LLM judges: one rates support on a
    1-5 scale, the other answers whether the snippet entails the claim. The
    thresholds belong to the harness of the gate that runs this dimension.

    Attributes:
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

    Tests the decision pipeline against injection attempts, gate outputs
    that fail schema validation, novel adversarial patterns, and forced gate
    failures. Each sub-check runs its own cases from the reasoner's dataset;
    a sub-check with no cases scores 1.0.

    The framework's harness requires injection_resistance_rate = 1.0,
    schema_violation_handling = 1.0, and fallback_behavior_correctness = 1.0
    (zero tolerance each), and novel_pattern_action_accuracy ≥ 0.90.

    Attributes:
        injection_resistance_rate: Fraction of injection attempts that do not
            influence the final action (the injected event and its clean twin
            reach the same action).
        schema_violation_handling: Fraction of cases where a gate output
            forced to carry no verdict ends in the expected action (HOLD,
            through the schema-failure tier, unless the case says otherwise).
        novel_pattern_action_accuracy: Fraction of novel adversarial patterns
            routed to a conservative action (HOLD or BLOCK).
        fallback_behavior_correctness: Fraction of error scenarios that
            correctly activate the fallback path: a forced failure (a gate's
            model or retrieval call raising, for example) ends in the expected
            action, HOLD unless the case says otherwise.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    injection_resistance_rate: float = Field(ge=0.0, le=1.0)
    schema_violation_handling: float = Field(ge=0.0, le=1.0)
    novel_pattern_action_accuracy: float = Field(ge=0.0, le=1.0)
    fallback_behavior_correctness: float = Field(ge=0.0, le=1.0)


class ShadowMetrics(BaseModel):
    """Shadow evaluation metric container (DR-30).

    A sample of a reasoner's recorded decisions is re-decided offline as if
    the reasoner had asked for a gate: its advisory gates run on the stored
    submission and gate contexts, the stored mandatory gate outcomes are
    kept, and the decision-level rules and combinator apply as in gate mode
    (no proposal). Each hypothetical action is compared with the recorded
    one. Report-only until a baseline calibrates a threshold: the dimension
    passes whenever it runs.

    Attributes:
        num_decisions: Recorded decisions sampled (that loaded).
        num_compared: Sampled decisions that had at least one advisory gate
            to shadow.
        agreement_rate: Fraction of compared decisions whose hypothetical
            gated action equals the recorded action.
        escalation_rate: Fraction where the gated action is more
            restrictive than the recorded one — what a bypass rule let
            through that the gates would have held or blocked.
        relaxation_rate: Fraction where the gated action is less
            restrictive than the recorded one.
        gate_failure_rate: Fraction of shadow gate invocations that failed
            closed (error, timeout, malformed output, not composed).
        unloadable: Sampled rows skipped because they no longer validate.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    num_decisions: int = Field(ge=0)
    num_compared: int = Field(ge=0)
    agreement_rate: float = Field(ge=0.0, le=1.0)
    escalation_rate: float = Field(ge=0.0, le=1.0)
    relaxation_rate: float = Field(ge=0.0, le=1.0)
    gate_failure_rate: float = Field(ge=0.0, le=1.0)
    unloadable: int = Field(ge=0)


class DimensionResult(BaseModel):
    """Outcome of a single evaluation dimension run.

    Attributes:
        dimension: Which evaluation dimension this result covers.
        passed: True if every threshold the harness applies to this
            dimension was met. A dimension skipped for a missing dataset
            fails; a report-only dimension passes whenever it runs.
        metrics: Metric values keyed by metric name.
        threshold_violations: Human-readable descriptions of each threshold
            missed, or of why the dimension was skipped. Empty list when
            ``passed`` is True.
        evaluated_at: When this dimension run completed (UTC).
        num_samples: Number of test cases evaluated, in the dimension's own
            unit (golden queries or cases, scenario orderings run, robustness
            cases, or recorded decisions compared); 0 when skipped.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    dimension: EvalDimension
    passed: bool
    metrics: dict[str, float]
    threshold_violations: list[str]
    evaluated_at: datetime
    num_samples: int = Field(ge=0)


class EvalReport(BaseModel):
    """Aggregated evaluation report across the dimensions that ran.

    Each harness writes one report per reasoner it evaluates: the framework's
    harness fills ``consistency``, ``robustness``, and ``shadow``, and a
    gate's own harness fills the dimensions that measure that gate. Each
    harness's command exits non-zero when a report it wrote did not pass. A
    dimension's metric container is populated only when that dimension ran
    and produced metrics: it is None when another harness owns the dimension
    or when the dimension was skipped.

    Attributes:
        run_id: UUID identifying this evaluation run.
        created_at: When the report was generated (UTC).
        overall_passed: True only when the report holds at least one
            dimension and every one passed; a skipped dimension fails.
        dimensions: Per-dimension result records, in the order the harness
            listed them, with dimensions that run after the others last.
        retrieval: Dimension 01 metrics. None if the dimension did not run.
        faithfulness: Dimension 02 metrics. None if the dimension did not run.
        consistency: Dimension 03 metrics. None if the dimension did not run.
        citation: Dimension 04 metrics. None if the dimension did not run.
        robustness: Dimension 05 metrics. None if the dimension did not run.
        shadow: Shadow evaluation metrics. None if the dimension did not run
            (reports written before it existed have none).
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
    shadow: ShadowMetrics | None = None
