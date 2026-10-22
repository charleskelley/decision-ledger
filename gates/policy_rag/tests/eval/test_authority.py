"""The authority-violation rate (DR-34): verdicts that defer to a lower tier.

A violation is a verdict naming a lower-tier corpus as governing while a
higher-tier passage on point was retrieved. Deterministic; reported with the
citation dimension when a reasoner ships ``authority/golden_outputs.yaml``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from core.eval.metrics import EvalDimension
from policy_rag_gate.eval.authority import (
    AuthorityCase,
    authority_violation,
    authority_violation_rate,
    load_authority_cases,
)
from policy_rag_gate.eval.citation import CitationDimension

if TYPE_CHECKING:
    from pathlib import Path


def _case(
    governing: str | None,
    on_point: list[str],
    case_id: str = "a01",
) -> AuthorityCase:
    return AuthorityCase.model_validate(
        {
            "case_id": case_id,
            "governing_corpus_id": governing,
            "contexts": [
                {"corpus_id": "law", "tier": 1, "document_id": "LAW-1"},
                {"corpus_id": "notes", "tier": 2, "document_id": "NOTE-1"},
            ],
            "on_point_document_ids": on_point,
        }
    )


def test_naming_the_lower_tier_while_the_higher_tier_is_on_point_violates():
    assert authority_violation(_case("notes", ["LAW-1", "NOTE-1"])) is True


def test_naming_the_highest_tier_on_point_does_not_violate():
    assert authority_violation(_case("law", ["LAW-1", "NOTE-1"])) is False


def test_naming_a_lower_tier_when_the_higher_one_is_off_point_does_not_violate():
    assert authority_violation(_case("notes", ["NOTE-1"])) is False


@pytest.mark.parametrize("governing", [None, "a-corpus-nothing-came-from"])
def test_without_a_governing_corpus_to_compare_there_is_no_violation(governing):
    assert authority_violation(_case(governing, ["LAW-1"])) is False


def test_the_rate_counts_only_verdicts_that_named_a_governing_corpus():
    cases = [
        _case("notes", ["LAW-1"], "a1"),  # violation
        _case("law", ["LAW-1"], "a2"),
        _case(None, ["LAW-1"], "a3"),  # named nothing: not counted
        _case("law", [], "a4"),
    ]

    assert authority_violation_rate(cases) == pytest.approx(1 / 3)
    assert authority_violation_rate([]) == 0.0
    assert authority_violation_rate([_case(None, [])]) == 0.0


def test_cases_load_from_a_golden_output_file(tmp_path: Path):
    path = tmp_path / "golden_outputs.yaml"
    path.write_text(
        "cases:\n"
        "  - case_id: a01\n"
        "    governing_corpus_id: notes\n"
        "    contexts:\n"
        "      - {corpus_id: law, tier: 1, document_id: LAW-1}\n"
        "    on_point_document_ids: [LAW-1]\n",
        encoding="utf-8",
    )

    (case,) = load_authority_cases(path)

    assert case.governing_corpus_id == "notes"
    assert case.contexts[0].tier == 1


def test_an_empty_file_holds_no_cases(tmp_path: Path):
    path = tmp_path / "golden_outputs.yaml"
    path.write_text("cases: []\n", encoding="utf-8")

    assert load_authority_cases(path) == []


class _Judge:
    """A judge that is never asked: no citation case is given."""

    async def complete_structured(self, **_: Any) -> Any:
        raise AssertionError("no citation case, no judge call")


async def test_the_citation_dimension_reports_the_rate_report_only():
    dimension = CitationDimension(
        client=_Judge(),  # type: ignore[arg-type]
        cases=[],
        authority_cases=[_case("notes", ["LAW-1"], "a1"), _case("law", ["LAW-1"])],
    )

    run = await dimension.evaluate()

    assert run.result.dimension == EvalDimension.CITATION
    assert run.result.metrics["authority_violation_rate"] == 0.5
    assert run.result.metrics["authority_cases"] == 2.0
    assert not any("authority" in v for v in run.result.threshold_violations)


async def test_without_authority_cases_the_rate_is_not_reported():
    run = await CitationDimension(client=_Judge(), cases=[]).evaluate()  # type: ignore[arg-type]

    assert "authority_violation_rate" not in run.result.metrics
