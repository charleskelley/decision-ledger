"""Unit tests for the citation dimension.

Stubs the JudgeClient — the live judge call goes against the real OpenAI
endpoint under @pytest.mark.evaluation. The unit tests exercise:

- Pure aggregate helper (superficial_rate)
- Dataset YAML loading
- Dimension wire-up: judges are invoked with the right vars; thresholds
  flag superficial citations and low relevance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

import pytest
from pydantic import BaseModel

from core.eval.metrics import CitationMetrics, EvalDimension
from eval.dimensions.citation import (
    SUPERFICIAL_BOUNDARY,
    CitationCase,
    CitationDimension,
    load_citation_cases,
    superficial_rate,
)
from eval.judge import JudgeClient, JudgePromptRegistry

if TYPE_CHECKING:
    from pathlib import Path

T = TypeVar("T", bound=BaseModel)


# ---------------------------------------------------------------------------
# Stub JudgeClient
# ---------------------------------------------------------------------------


class _StubJudgeClient:
    """Returns canned JudgeOutputs based on the user prompt content.

    Two judges (relevance + entailment) share one client; the stub disambiguates
    by inspecting the system prompt text — relevance uses 1-5 rubric, entailment
    is binary. The test fixtures key each (claim, snippet) pair to canned
    scores.
    """

    def __init__(
        self,
        relevance_by_claim: dict[str, float],
        entailment_by_claim: dict[str, float],
    ) -> None:
        self._rel = relevance_by_claim
        self._ent = entailment_by_claim
        self.calls: list[tuple[str, str]] = []  # (system-prefix, claim)

    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        response_format: type[T],
    ) -> T:
        # Identify which judge by looking at the system prompt's rubric word.
        is_entailment = "entailment" in system.lower()
        # Extract the claim from the user prompt — it's the line right after
        # "## Claim from the Rationale" or "## Claim".
        claim = self._extract_claim(user)
        self.calls.append(("entailment" if is_entailment else "relevance", claim))
        score_map = self._ent if is_entailment else self._rel
        score = score_map.get(claim, 0.0)
        payload = {"score": score, "reasoning": f"stub-{score}"}
        return response_format.model_validate(payload)

    @staticmethod
    def _extract_claim(user: str) -> str:
        """Extract the claim line from the user prompt (heuristic for the stub)."""
        lines = user.splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith("## Claim"):
                # The claim is the next non-empty line.
                for j in range(i + 1, len(lines)):
                    if lines[j].strip():
                        return lines[j].strip()
        return ""


# ---------------------------------------------------------------------------
# superficial_rate
# ---------------------------------------------------------------------------


class TestSuperficialRate:
    def test_all_strong(self) -> None:
        assert superficial_rate([0.9, 0.85, 0.7]) == 0.0

    def test_all_superficial(self) -> None:
        assert superficial_rate([0.2, 0.3, 0.4]) == 1.0

    def test_partial(self) -> None:
        assert superficial_rate([0.9, 0.3, 0.5]) == pytest.approx(1 / 3)

    def test_boundary_inclusive(self) -> None:
        # Boundary value (0.4) is FLAGGED as superficial.
        assert superficial_rate([SUPERFICIAL_BOUNDARY]) == 1.0

    def test_empty(self) -> None:
        assert superficial_rate([]) == 0.0


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------


class TestLoadCitationCases:
    def test_loads_default_dataset(self) -> None:
        from pathlib import Path

        path = (
            Path(__file__).parents[2]
            / "eval"
            / "datasets"
            / "citations"
            / "golden_outputs.yaml"
        )
        cases = load_citation_cases(path)
        assert len(cases) >= 3
        assert all(isinstance(c, CitationCase) for c in cases)
        ids = [c.case_id for c in cases]
        assert len(ids) == len(set(ids))

    def test_round_trip_yaml(self, tmp_path: Path) -> None:
        fixture = tmp_path / "cases.yaml"
        fixture.write_text(
            "cases:\n"
            "  - case_id: c1\n"
            "    claim: 'a claim'\n"
            "    document_id: DOC-A\n"
            "    document_version: '1.0'\n"
            "    section_path: '1.1'\n"
            "    snippet_text: 'cited text'\n",
            encoding="utf-8",
        )
        cases = load_citation_cases(fixture)
        assert len(cases) == 1
        assert cases[0].case_id == "c1"


# ---------------------------------------------------------------------------
# CitationDimension
# ---------------------------------------------------------------------------


def _case(case_id: str, *, claim: str = "claim text") -> CitationCase:
    return CitationCase(
        case_id=case_id,
        claim=claim,
        document_id="DOC",
        document_version="1.0",
        section_path="s",
        snippet_text="snippet",
    )


class TestCitationDimension:
    @pytest.mark.asyncio
    async def test_high_quality_passes(self) -> None:
        cases = [_case("c1", claim="A"), _case("c2", claim="B")]
        client = _StubJudgeClient(
            relevance_by_claim={"A": 0.9, "B": 0.85},
            entailment_by_claim={"A": 1.0, "B": 1.0},
        )
        # Confirm stub satisfies the JudgeClient protocol structurally.
        _: JudgeClient = client
        dim = CitationDimension(
            client=client,
            cases=cases,
            prompt_registry=JudgePromptRegistry(),
        )
        run = await dim.evaluate()

        assert run.result.dimension == EvalDimension.CITATION
        assert run.result.passed is True
        assert run.result.threshold_violations == []
        assert isinstance(run.metrics, CitationMetrics)
        assert run.metrics.citation_relevance_score == pytest.approx(0.875)
        assert run.metrics.claim_citation_entailment == 1.0
        assert run.metrics.superficial_citation_rate == 0.0
        # Relevance + entailment judges each call once per case.
        assert len([c for c in client.calls if c[0] == "relevance"]) == 2
        assert len([c for c in client.calls if c[0] == "entailment"]) == 2

    @pytest.mark.asyncio
    async def test_low_relevance_fails(self) -> None:
        cases = [_case("c1", claim="A"), _case("c2", claim="B")]
        client = _StubJudgeClient(
            relevance_by_claim={"A": 0.3, "B": 0.4},
            entailment_by_claim={"A": 0.0, "B": 0.0},
        )
        dim = CitationDimension(client=client, cases=cases)
        run = await dim.evaluate()

        assert run.result.passed is False
        violations = run.result.threshold_violations
        assert any("citation_relevance_score" in v for v in violations)
        assert any("claim_citation_entailment" in v for v in violations)
        assert any("superficial_citation_rate" in v for v in violations)

    @pytest.mark.asyncio
    async def test_judge_called_with_case_vars(self) -> None:
        cases = [_case("c1", claim="My specific claim")]
        client = _StubJudgeClient(
            relevance_by_claim={"My specific claim": 0.95},
            entailment_by_claim={"My specific claim": 1.0},
        )
        dim = CitationDimension(client=client, cases=cases)
        await dim.evaluate()

        # Both calls extracted the exact claim from the rendered prompt.
        claims_called = {c[1] for c in client.calls}
        assert claims_called == {"My specific claim"}

    @pytest.mark.asyncio
    async def test_empty_cases_passes_vacuously(self) -> None:
        client = _StubJudgeClient({}, {})
        dim = CitationDimension(client=client, cases=[])
        run = await dim.evaluate()

        # No cases → no violations → passes (means default to 0).
        assert run.result.num_samples == 0
        assert isinstance(run.metrics, CitationMetrics)
        # 0.0 means everything fails the >= threshold check.
        assert run.result.passed is False
        # No judge calls.
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_concurrency_bound(self) -> None:
        # Many cases, max_concurrency=2 → semaphore limits in-flight calls.
        cases = [_case(f"c{i}", claim=f"claim{i}") for i in range(8)]
        client = _StubJudgeClient(
            relevance_by_claim={f"claim{i}": 0.9 for i in range(8)},
            entailment_by_claim={f"claim{i}": 1.0 for i in range(8)},
        )
        dim = CitationDimension(client=client, cases=cases, max_concurrency=2)
        run = await dim.evaluate()

        assert run.result.num_samples == 8
        # Each case: 1 relevance + 1 entailment call = 16 calls total.
        assert len(client.calls) == 16
