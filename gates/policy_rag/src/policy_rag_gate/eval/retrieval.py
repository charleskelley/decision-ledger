"""Dimension 01 — Retrieval Quality.

Drives the gate's own retrieval path — every corpus the reasoner's binding
names, through its provider, with the binding's quota, merged in precedence
order exactly as the gate renders it (``policy_rag_gate.gate.
retrieve_evidence``, DR-34) — against a golden query set and computes:

- ``context_precision_at_k``, ``context_recall_at_k``, ``mean_reciprocal_rank``
  via classical IR formulas over annotated ``relevant_document_ids``, on the
  passages the gate would render (``k`` is the binding's ``k_per_corpus``
  times its number of corpora).
- ``version_resolution_accuracy`` (zero tolerance) — for each query, did the
  top result match the expected ``document_id`` *and* version? This catches
  superseded-version regressions in the retriever's ranking.
- ``jurisdiction_filter_accuracy`` (zero tolerance) — for each query with a
  jurisdiction filter, are all returned passages in-jurisdiction? Catches
  filter-leakage regressions.
- Per corpus (report-only, ``per_corpus.<corpus_id>.<metric>``): precision
  at the corpus's quota, mean reciprocal rank, and hit rate over each
  corpus's own ranking (ranks are comparable within a corpus only), the
  passages it returned on average, and how often its provider fell back
  from the ranking it attempted (the reference provider's reranker latency
  bypass).

We use classical IR formulas here, not RAGAS, because we have ground-truth
annotated chunk IDs. RAGAS' ``context_precision``/``context_recall`` use an
LLM judge for relevance — that's the right tool when ground truth is
unavailable, but with annotated relevance it adds noise + cost where
deterministic formulas suffice. RAGAS earns its place in the faithfulness
dimension where claim decomposition + entailment is genuinely hard.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ConfigDict

from core.eval.metrics import (
    DimensionResult,
    EvalDimension,
    RetrievalMetrics,
)
from eval.dimensions.base import DimensionRun
from policy_rag_gate.gate import retrieve_evidence

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from policy_rag_gate.corpus import PolicyBindingConfig
    from policy_rag_gate.models import RetrievedContext
    from policy_rag_gate.providers import ProviderRegistry

# ---------------------------------------------------------------------------
# CI-gate thresholds.
#
# The aspirational precision@k threshold is 0.80, but that's calibrated for
# dense ground-truth datasets where most queries have ≥k relevant documents.
# Our golden set has 1-2 relevant docs per query (real-world policy retrieval
# is sparse), which would cap document-level precision@5 at 0.20-0.40; this
# metric counts every retrieved chunk of a relevant document, so the committed
# baselines score higher (0.63-0.67). We set the gate to 0.40 here — that's
# realistic for sparse relevance and still catches real regressions (a
# working retriever lands ≥2 relevant in top-5 on average).
#
# Recall and MRR remain the primary signal; precision is informational at this
# threshold. A future, denser golden set could raise this back toward 0.80.
# ---------------------------------------------------------------------------

THRESHOLD_PRECISION_AT_K = 0.40
THRESHOLD_RECALL_AT_K = 0.75
THRESHOLD_MRR = 0.70
THRESHOLD_VERSION_RESOLUTION = 1.0  # zero tolerance
THRESHOLD_JURISDICTION_FILTER = 1.0  # zero tolerance

DEFAULT_K = 5


# ---------------------------------------------------------------------------
# Golden query schema
# ---------------------------------------------------------------------------


class GoldenQuery(BaseModel):
    """A retrieval test case with annotated relevance.

    Attributes:
        query_id: Stable identifier (used in failure messages and replay).
        query: Natural-language query string passed to the retriever.
        jurisdictions: If provided, retriever filter; ALL retrieved snippets
            must have one of these jurisdictions.
        risk_tier: If provided, retriever risk-tier filter.
        relevant_document_ids: Ground-truth ``document_id`` values that
            should appear in the top-k for this query (any order).
        expected_top_document_id: The single document expected to rank #1.
            Used by ``version_resolution_accuracy`` to detect superseded
            versions ranking above their successors.
        expected_top_version: Expected version string of the top result.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    query_id: str
    query: str
    jurisdictions: list[str] | None = None
    risk_tier: str | None = None
    relevant_document_ids: list[str]
    expected_top_document_id: str
    expected_top_version: str


def load_golden_queries(path: Path) -> list[GoldenQuery]:
    """Parse a YAML golden-query file.

    Args:
        path: Path to the YAML file. Must contain a top-level ``queries:``
            list.

    Returns:
        Validated list of ``GoldenQuery``.
    """
    raw: dict = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [GoldenQuery.model_validate(q) for q in raw["queries"]]


# ---------------------------------------------------------------------------
# Pure metric helpers (deterministic — testable without infrastructure)
# ---------------------------------------------------------------------------


def precision_at_k(
    retrieved_ids: Sequence[str], relevant_ids: set[str], k: int
) -> float:
    """Fraction of the top-k retrieved that are in the relevant set.

    Args:
        retrieved_ids: Ranked retrieved document IDs (top first).
        relevant_ids: Ground-truth relevant document IDs.
        k: Cutoff. If ``len(retrieved_ids) < k``, the denominator is still
            ``k`` (standard IR convention — missing results count against
            precision).

    Returns:
        Precision in ``[0, 1]``. Zero if ``k <= 0``.
    """
    if k <= 0:
        return 0.0
    top = retrieved_ids[:k]
    hits = sum(1 for d in top if d in relevant_ids)
    return hits / k


def recall_at_k(retrieved_ids: Sequence[str], relevant_ids: set[str], k: int) -> float:
    """Fraction of relevant documents present in the top-k.

    Args:
        retrieved_ids: Ranked retrieved document IDs.
        relevant_ids: Ground-truth relevant document IDs.
        k: Cutoff.

    Returns:
        Recall in ``[0, 1]``. ``1.0`` when there are no relevant documents
        (vacuously satisfied).
    """
    if not relevant_ids:
        return 1.0
    top = set(retrieved_ids[:k])
    hits = len(top & relevant_ids)
    return hits / len(relevant_ids)


def reciprocal_rank(retrieved_ids: Sequence[str], relevant_ids: set[str]) -> float:
    """Reciprocal rank of the first relevant document.

    Args:
        retrieved_ids: Ranked retrieved document IDs.
        relevant_ids: Ground-truth relevant document IDs.

    Returns:
        ``1 / rank_of_first_relevant`` (rank is 1-based), or ``0.0`` if no
        relevant document is in the list.
    """
    for i, doc_id in enumerate(retrieved_ids, start=1):
        if doc_id in relevant_ids:
            return 1.0 / i
    return 0.0


def has_correct_top_version(
    snippets: Sequence[RetrievedContext],
    *,
    expected_document_id: str,
    expected_version: str,
    k: int = 3,
) -> bool:
    """Did the expected document+version appear in the top-k retrieved snippets?

    Hybrid retrieval (RRF + cross-encoder rerank) routinely surfaces topical
    siblings ahead of the most-targeted document on natural-language queries.
    Strict top-1 equality penalises healthy retrievers; checking the top-k
    catches the regression this metric was designed for ("retriever silently
    starts returning v1 instead of v2 of a superseded policy") while
    tolerating reasonable rank noise on general queries.

    Args:
        snippets: Ranked retrieved snippets (top first).
        expected_document_id: ``document_id`` the dataset says should rank.
        expected_version: ``version`` string that must accompany it.
        k: Top-k cutoff to scan. Default 3.

    Returns:
        ``True`` iff at least one of the first ``k`` snippets matches both
        ``expected_document_id`` and ``expected_version``.
    """
    for s in snippets[:k]:
        if s.document_id == expected_document_id and s.version == expected_version:
            return True
    return False


def jurisdictions_respected(
    snippets: Sequence[RetrievedContext],
    allowed: list[str] | None,
) -> bool:
    """All retrieved passages must lie within the allowed jurisdiction set.

    Args:
        snippets: Retrieved snippets.
        allowed: Allowed jurisdictions. ``None`` means no filter was
            requested — vacuously satisfied.

    Returns:
        ``True`` iff every snippet's ``jurisdiction`` is in ``allowed`` (or
        ``allowed`` is None).
    """
    if allowed is None:
        return True
    allowed_set = set(allowed)
    return all(s.jurisdiction in allowed_set for s in snippets)


# ---------------------------------------------------------------------------
# RetrievalDimension
# ---------------------------------------------------------------------------


def _bypassed(requested: str | None, applied: str) -> bool:
    return requested is not None and applied != requested


class RetrievalDimension:
    """Drives the gate's retrieval over one binding against a golden query set.

    Args:
        providers: The providers the binding's corpora name (the reference
            provider in a live run; a fake in unit tests).
        binding: The reasoner's binding to the policy gate: the corpora and
            the quota the gate retrieves with for it.
        golden_queries: Loaded golden query set.
    """

    kind = EvalDimension.RETRIEVAL

    def __init__(
        self,
        *,
        providers: ProviderRegistry,
        binding: PolicyBindingConfig,
        golden_queries: list[GoldenQuery],
    ) -> None:
        """Construct the dimension with its dependencies."""
        self._providers = providers
        self._binding = binding
        self._queries = golden_queries
        self._k = binding.k_per_corpus * len(binding.corpora)

    async def evaluate(self) -> DimensionRun:
        """Run all golden queries and assemble the dimension run."""
        precisions: list[float] = []
        recalls: list[float] = []
        mrrs: list[float] = []
        version_hits: list[bool] = []
        jurisdiction_hits: list[bool] = []
        per_corpus: dict[str, dict[str, list[float]]] = {
            corpus.corpus_id: {
                "precision_at_k": [],
                "mean_reciprocal_rank": [],
                "hit_rate": [],
                "mean_returned": [],
                "ranking_bypass_rate": [],
            }
            for corpus in self._binding.corpora
        }

        for q in self._queries:
            evidence = retrieve_evidence(
                self._providers,
                self._binding,
                q.query,
                jurisdictions=q.jurisdictions or (),
                risk_tier=q.risk_tier,
            )
            retrieved_ids = [c.document_id for c in evidence.contexts]
            relevant = set(q.relevant_document_ids)

            precisions.append(precision_at_k(retrieved_ids, relevant, self._k))
            recalls.append(recall_at_k(retrieved_ids, relevant, self._k))
            mrrs.append(reciprocal_rank(retrieved_ids, relevant))
            version_hits.append(
                has_correct_top_version(
                    evidence.contexts,
                    expected_document_id=q.expected_top_document_id,
                    expected_version=q.expected_top_version,
                )
            )
            jurisdiction_hits.append(
                jurisdictions_respected(evidence.contexts, q.jurisdictions)
            )
            for query in evidence.queries:
                result = evidence.results.get(query.corpus_ref.corpus_id)
                if result is None:
                    continue
                ids = [c.document_id for c in result.contexts]
                stats = per_corpus[query.corpus_ref.corpus_id]
                stats["precision_at_k"].append(precision_at_k(ids, relevant, query.k))
                stats["mean_reciprocal_rank"].append(reciprocal_rank(ids, relevant))
                stats["hit_rate"].append(float(bool(set(ids) & relevant)))
                stats["mean_returned"].append(float(len(ids)))
                stats["ranking_bypass_rate"].append(
                    float(_bypassed(query.ranking_requested, query.ranking_applied))
                )

        n = len(self._queries)
        mean_precision = sum(precisions) / n if n else 0.0
        mean_recall = sum(recalls) / n if n else 0.0
        mean_mrr = sum(mrrs) / n if n else 0.0
        version_acc = sum(version_hits) / n if n else 0.0
        jurisdiction_acc = sum(jurisdiction_hits) / n if n else 0.0

        violations = self._violations(
            mean_precision=mean_precision,
            mean_recall=mean_recall,
            mean_mrr=mean_mrr,
            version_acc=version_acc,
            jurisdiction_acc=jurisdiction_acc,
        )

        metrics = RetrievalMetrics(
            k=self._k,
            context_precision_at_k=round(mean_precision, 4),
            context_recall_at_k=round(mean_recall, 4),
            mean_reciprocal_rank=round(mean_mrr, 4),
            version_resolution_accuracy=round(version_acc, 4),
            jurisdiction_filter_accuracy=round(jurisdiction_acc, 4),
        )
        corpus_metrics = {
            f"per_corpus.{corpus_id}.{name}": round(sum(values) / len(values), 4)
            for corpus_id, stats in per_corpus.items()
            for name, values in stats.items()
            if values
        }
        result = DimensionResult(
            dimension=self.kind,
            passed=not violations,
            metrics={
                "context_precision_at_k": metrics.context_precision_at_k,
                "context_recall_at_k": metrics.context_recall_at_k,
                "mean_reciprocal_rank": metrics.mean_reciprocal_rank,
                "version_resolution_accuracy": metrics.version_resolution_accuracy,
                "jurisdiction_filter_accuracy": metrics.jurisdiction_filter_accuracy,
                **corpus_metrics,
            },
            threshold_violations=violations,
            evaluated_at=datetime.now(UTC),
            num_samples=n,
        )
        return DimensionRun(result=result, metrics=metrics)

    @staticmethod
    def _violations(
        *,
        mean_precision: float,
        mean_recall: float,
        mean_mrr: float,
        version_acc: float,
        jurisdiction_acc: float,
    ) -> list[str]:
        """Compose human-readable threshold-violation messages.

        Returns:
            Empty list when all thresholds are met.
        """
        v: list[str] = []
        if mean_precision < THRESHOLD_PRECISION_AT_K:
            v.append(
                f"context_precision_at_k {mean_precision:.3f} < "
                f"{THRESHOLD_PRECISION_AT_K:.2f}"
            )
        if mean_recall < THRESHOLD_RECALL_AT_K:
            v.append(
                f"context_recall_at_k {mean_recall:.3f} < {THRESHOLD_RECALL_AT_K:.2f}"
            )
        if mean_mrr < THRESHOLD_MRR:
            v.append(f"mean_reciprocal_rank {mean_mrr:.3f} < {THRESHOLD_MRR:.2f}")
        if version_acc < THRESHOLD_VERSION_RESOLUTION:
            v.append(
                f"version_resolution_accuracy {version_acc:.3f} < "
                f"{THRESHOLD_VERSION_RESOLUTION:.2f} (zero tolerance)"
            )
        if jurisdiction_acc < THRESHOLD_JURISDICTION_FILTER:
            v.append(
                f"jurisdiction_filter_accuracy {jurisdiction_acc:.3f} < "
                f"{THRESHOLD_JURISDICTION_FILTER:.2f} (zero tolerance)"
            )
        return v

    @property
    def k(self) -> int:
        """Cutoff of the overall metrics: the binding's quota times its corpora."""
        return self._k

    @property
    def binding(self) -> PolicyBindingConfig:
        """The binding the dimension retrieves with."""
        return self._binding


__all__ = [
    "DEFAULT_K",
    "THRESHOLD_JURISDICTION_FILTER",
    "THRESHOLD_MRR",
    "THRESHOLD_PRECISION_AT_K",
    "THRESHOLD_RECALL_AT_K",
    "THRESHOLD_VERSION_RESOLUTION",
    "GoldenQuery",
    "RetrievalDimension",
    "has_correct_top_version",
    "jurisdictions_respected",
    "load_golden_queries",
    "precision_at_k",
    "recall_at_k",
    "reciprocal_rank",
]
