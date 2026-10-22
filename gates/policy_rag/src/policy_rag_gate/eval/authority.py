"""Authority violations — did a verdict defer to the highest authority on point?

A reasoner may bind the policy gate to several corpora at different
authority tiers (DR-34), tier 1 the highest. The gate renders evidence by
tier and a template with the conflict/gap protocol asks the model to name
the governing corpus. An *authority violation* is a verdict that names a
lower-tier corpus as governing while a higher-tier passage on point was
retrieved: research on retrieval-augmented models finds they adopt wrong
retrieved text often and recognize conflicts rarely, so this is the failure
the tiered layout exists to prevent, measured here.

Deterministic (no LLM): it reads captured gate outputs from a reasoner's
``authority/golden_outputs.yaml`` — each case the corpora and tiers of the
passages the gate rendered, the corpus the verdict named as governing, and
the passages on point (annotated, or, for a live capture, the documents the
verdict cites). ``authority_violation_rate`` is reported with the citation
dimension (``CitationDimension``), since it is about what a verdict cites
as governing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class AuthorityContext(BaseModel):
    """One rendered passage: where it came from and at what tier.

    Attributes:
        corpus_id: Its corpus.
        tier: The corpus's tier in the binding (1 is highest).
        document_id: Its document.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    corpus_id: str
    tier: int = Field(ge=1)
    document_id: str


class AuthorityCase(BaseModel):
    """One gate output, reduced to what authority needs.

    Attributes:
        case_id: Stable identifier.
        governing_corpus_id: The corpus the verdict named as governing, or
            ``None`` when it named none.
        contexts: The passages the gate rendered.
        on_point_document_ids: The documents with a passage on point
            (annotated; a live capture uses the documents the verdict
            cites).
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    case_id: str
    governing_corpus_id: str | None
    contexts: list[AuthorityContext]
    on_point_document_ids: list[str]


def authority_violation(case: AuthorityCase) -> bool:
    """Whether the verdict named a lower tier than a passage on point had.

    Args:
        case: One gate output.

    Returns:
        ``True`` when a passage on point comes from a higher tier (a lower
        number) than the governing corpus's. ``False`` when the verdict
        named no governing corpus, or one none of the passages came from
        (there is no tier to compare).
    """
    if case.governing_corpus_id is None:
        return False
    tiers = {
        context.corpus_id: context.tier
        for context in case.contexts
        if context.corpus_id == case.governing_corpus_id
    }
    governing_tier = tiers.get(case.governing_corpus_id)
    if governing_tier is None:
        return False
    on_point = set(case.on_point_document_ids)
    return any(
        context.tier < governing_tier
        for context in case.contexts
        if context.document_id in on_point
    )


def authority_violation_rate(cases: Sequence[AuthorityCase]) -> float:
    """Fraction of verdicts naming a governing corpus that violate authority.

    Args:
        cases: Captured gate outputs.

    Returns:
        Violations over the cases whose verdict named a governing corpus;
        ``0.0`` when none did.
    """
    named = [case for case in cases if case.governing_corpus_id is not None]
    if not named:
        return 0.0
    return sum(authority_violation(case) for case in named) / len(named)


def load_authority_cases(path: Path) -> list[AuthorityCase]:
    """Parse an authority golden-output file (a top-level ``cases:`` list).

    Args:
        path: The YAML file.

    Returns:
        The validated cases.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [AuthorityCase.model_validate(case) for case in raw.get("cases") or []]


__all__ = [
    "AuthorityCase",
    "AuthorityContext",
    "authority_violation",
    "authority_violation_rate",
    "load_authority_cases",
]
