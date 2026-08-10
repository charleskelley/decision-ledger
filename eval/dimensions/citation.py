"""Dimension 04 — Citation Accuracy.

For each (claim, cited_snippet) pair in the golden output set:

- ``citation_relevance_score`` — judge prompt rates 1-5 on whether the
  cited snippet supports the claim, normalized to [0, 1].
- ``claim_citation_entailment`` — separate judge prompt asks yes/no
  whether the snippet entails the claim. Mean = fraction of yeses.
- ``superficial_citation_rate`` — fraction of citations with relevance
  score ≤ 0.4 (≤ 2/5 on the original scale).

Both judges go through the SDK-agnostic ``LLMClient`` protocol from
``core.llm`` (DR-23).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ConfigDict

from core.eval.metrics import (
    CitationMetrics,
    DimensionResult,
    EvalDimension,
)
from eval.dimensions.base import DimensionRun
from eval.judge import (
    JudgePromptRegistry,
    JudgePromptTemplate,
    llm_judge,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.llm import LLMClient

# ---------------------------------------------------------------------------
# CI-gate thresholds (mirror core/eval/metrics.py:CitationMetrics docstring)
# ---------------------------------------------------------------------------

THRESHOLD_RELEVANCE = 0.80
THRESHOLD_ENTAILMENT = 0.70  # not in core docstring; defensible MVP default
THRESHOLD_SUPERFICIAL_RATE_MAX = 0.05  # ≤ 5% may be superficial

# Boundary at which a relevance score is "superficial" (judge mapped 1-5 → [0,1];
# scores ≤ 2/5 = 0.4 are flagged).
SUPERFICIAL_BOUNDARY = 0.4

_DATASETS_DIR = Path(__file__).parents[1] / "datasets" / "citations"
_RELEVANCE_TEMPLATE_ID = "citation_relevance"
_ENTAILMENT_TEMPLATE_ID = "citation_entailment"


# ---------------------------------------------------------------------------
# Golden dataset schema
# ---------------------------------------------------------------------------


class CitationCase(BaseModel):
    """One (claim, cited snippet) pair from the golden output set.

    Attributes:
        case_id: Stable identifier (used in failure messages).
        claim: A single rationale claim the citation should support.
        document_id: Cited document's ID.
        document_version: Cited document's version.
        section_path: Cited document's section.
        snippet_text: Verbatim cited text.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    case_id: str
    claim: str
    document_id: str
    document_version: str
    section_path: str
    snippet_text: str


def load_citation_cases(path: Path) -> list[CitationCase]:
    """Parse a YAML golden output file into citation cases.

    File shape:

    ```yaml
    cases:
      - case_id: ...
        claim: ...
        document_id: ...
        document_version: ...
        section_path: ...
        snippet_text: ...
    ```
    """
    raw: dict = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [CitationCase.model_validate(c) for c in raw["cases"]]


# ---------------------------------------------------------------------------
# Pure aggregate helpers
# ---------------------------------------------------------------------------


def superficial_rate(relevance_scores: Sequence[float]) -> float:
    """Fraction of relevance scores at or below the superficial boundary.

    Args:
        relevance_scores: Per-citation relevance scores in [0, 1].

    Returns:
        Fraction in [0, 1]; 0.0 on empty input.
    """
    if not relevance_scores:
        return 0.0
    flagged = sum(1 for s in relevance_scores if s <= SUPERFICIAL_BOUNDARY)
    return flagged / len(relevance_scores)


# ---------------------------------------------------------------------------
# CitationDimension
# ---------------------------------------------------------------------------


class CitationDimension:
    """Drives the citation judges over a golden output set.

    Args:
        client: SDK-agnostic ``LLMClient`` implementation.
        cases: Loaded ``CitationCase`` set.
        prompt_registry: Optional prompt registry override (for tests).
        max_concurrency: Maximum number of concurrent judge calls. Bounded
            to avoid hitting LLM rate limits.
    """

    kind = EvalDimension.CITATION

    def __init__(
        self,
        *,
        client: LLMClient,
        cases: list[CitationCase],
        prompt_registry: JudgePromptRegistry | None = None,
        max_concurrency: int = 4,
    ) -> None:
        """Construct the dimension."""
        self._client = client
        self._cases = cases
        self._registry = prompt_registry or JudgePromptRegistry()
        self._sem = asyncio.Semaphore(max_concurrency)

    @classmethod
    def from_default_dataset(
        cls,
        *,
        client: LLMClient,
        max_concurrency: int = 4,
    ) -> CitationDimension:
        """Construct using the bundled ``golden_outputs.yaml`` dataset."""
        path = _DATASETS_DIR / "golden_outputs.yaml"
        cases = load_citation_cases(path) if path.exists() else []
        return cls(client=client, cases=cases, max_concurrency=max_concurrency)

    async def evaluate(self) -> DimensionRun:
        """Run both judges on every case and assemble the dimension run."""
        relevance_template = self._registry.get(_RELEVANCE_TEMPLATE_ID)
        entailment_template = self._registry.get(_ENTAILMENT_TEMPLATE_ID)

        relevance_scores, entailment_scores = await self._score_all(
            relevance_template=relevance_template,
            entailment_template=entailment_template,
        )

        n = len(self._cases)
        mean_relevance = sum(relevance_scores) / n if n else 0.0
        mean_entailment = sum(entailment_scores) / n if n else 0.0
        superficial = superficial_rate(relevance_scores)

        violations = self._violations(
            mean_relevance=mean_relevance,
            mean_entailment=mean_entailment,
            superficial=superficial,
        )

        metrics = CitationMetrics(
            citation_relevance_score=round(mean_relevance, 4),
            claim_citation_entailment=round(mean_entailment, 4),
            superficial_citation_rate=round(superficial, 4),
        )
        result = DimensionResult(
            dimension=self.kind,
            passed=not violations,
            metrics={
                "citation_relevance_score": metrics.citation_relevance_score,
                "claim_citation_entailment": metrics.claim_citation_entailment,
                "superficial_citation_rate": metrics.superficial_citation_rate,
            },
            threshold_violations=violations,
            evaluated_at=datetime.now(UTC),
            num_samples=n,
        )
        return DimensionRun(result=result, metrics=metrics)

    async def _score_all(
        self,
        *,
        relevance_template: JudgePromptTemplate,
        entailment_template: JudgePromptTemplate,
    ) -> tuple[list[float], list[float]]:
        """Run both judges concurrently across all cases."""

        async def _one(case: CitationCase) -> tuple[float, float]:
            async with self._sem:
                vars_ = {
                    "claim": case.claim,
                    "document_id": case.document_id,
                    "document_version": case.document_version,
                    "section_path": case.section_path,
                    "snippet_text": case.snippet_text,
                }
                relevance_out, entailment_out = await asyncio.gather(
                    llm_judge(
                        template=relevance_template,
                        template_vars=vars_,
                        client=self._client,
                    ),
                    llm_judge(
                        template=entailment_template,
                        template_vars=vars_,
                        client=self._client,
                    ),
                )
                return relevance_out.score, entailment_out.score

        if not self._cases:
            return [], []
        results = await asyncio.gather(*(_one(c) for c in self._cases))
        relevance_scores = [r for r, _ in results]
        entailment_scores = [e for _, e in results]
        return relevance_scores, entailment_scores

    @staticmethod
    def _violations(
        *,
        mean_relevance: float,
        mean_entailment: float,
        superficial: float,
    ) -> list[str]:
        """Compose threshold-violation messages."""
        v: list[str] = []
        if mean_relevance < THRESHOLD_RELEVANCE:
            v.append(
                f"citation_relevance_score {mean_relevance:.3f} < "
                f"{THRESHOLD_RELEVANCE:.2f}"
            )
        if mean_entailment < THRESHOLD_ENTAILMENT:
            v.append(
                f"claim_citation_entailment {mean_entailment:.3f} < "
                f"{THRESHOLD_ENTAILMENT:.2f}"
            )
        if superficial > THRESHOLD_SUPERFICIAL_RATE_MAX:
            v.append(
                f"superficial_citation_rate {superficial:.3f} > "
                f"{THRESHOLD_SUPERFICIAL_RATE_MAX:.2f}"
            )
        return v


__all__ = [
    "SUPERFICIAL_BOUNDARY",
    "THRESHOLD_ENTAILMENT",
    "THRESHOLD_RELEVANCE",
    "THRESHOLD_SUPERFICIAL_RATE_MAX",
    "CitationCase",
    "CitationDimension",
    "load_citation_cases",
    "superficial_rate",
]
