"""Eval harness runner.

Orchestrates a list of ``Dimension`` implementations into a single
``EvalReport``. Writes the report as JSON, exits non-zero on any threshold
violation — that's the merge gate.

Dimensions are constructed and passed in by the caller (CLI entrypoint or
test harness). The runner does not know which dimensions exist; it only
asks each one to ``evaluate()`` and assembles the report. This keeps the
runner stable as new dimensions are added.

Usage::

    from eval.runners.harness import run_eval

    report = await run_eval(dimensions=[...], output_path=Path("report.json"))
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from openai import AsyncOpenAI

from app.settings import Settings
from core.eval.metrics import (
    CitationMetrics,
    ConsistencyMetrics,
    EvalDimension,
    EvalReport,
    FaithfulnessMetrics,
    RetrievalMetrics,
    RobustnessMetrics,
)
from eval.clients.openai import OpenAIJudgeClient
from eval.clients.pipeline import PipelineDriver
from eval.clients.ragas import RagasFaithfulnessAdapter
from eval.dimensions.citation import CitationDimension, load_citation_cases
from eval.dimensions.consistency import ConsistencyDimension, load_scenarios
from eval.dimensions.faithfulness import (
    FaithfulnessDimension,
    load_faithfulness_cases,
)
from eval.dimensions.retrieval import RetrievalDimension, load_golden_queries
from eval.dimensions.robustness import (
    RobustnessDimension,
    load_fallback_cases,
    load_injection_cases,
    load_novel_pattern_cases,
    load_schema_violation_cases,
)
from eval.dimensions.skipped import SkippedDimension

if TYPE_CHECKING:
    from collections.abc import Sequence

    from eval.dimensions import Dimension, DimensionRun

log = structlog.get_logger(__name__)


def assemble_report(*, run_id: str, runs: Sequence[DimensionRun]) -> EvalReport:
    """Compose dimension runs into a single ``EvalReport``.

    Pure — no I/O, no clock side-effects beyond ``datetime.now(UTC)`` for the
    report's ``created_at``.

    Args:
        run_id: Unique ID for this evaluation run (typically a UUID).
        runs: All dimension runs to include in the report.

    Returns:
        Fully populated ``EvalReport``. ``overall_passed`` is True only if
        every run's ``result.passed`` is True.
    """
    retrieval: RetrievalMetrics | None = None
    faithfulness: FaithfulnessMetrics | None = None
    consistency: ConsistencyMetrics | None = None
    citation: CitationMetrics | None = None
    robustness: RobustnessMetrics | None = None

    for run in runs:
        match run.metrics:
            case RetrievalMetrics():
                retrieval = run.metrics
            case FaithfulnessMetrics():
                faithfulness = run.metrics
            case ConsistencyMetrics():
                consistency = run.metrics
            case CitationMetrics():
                citation = run.metrics
            case RobustnessMetrics():
                robustness = run.metrics
            case None:
                pass

    overall = all(run.result.passed for run in runs) if runs else False

    return EvalReport(
        run_id=run_id,
        created_at=datetime.now(UTC),
        overall_passed=overall,
        dimensions=[run.result for run in runs],
        retrieval=retrieval,
        faithfulness=faithfulness,
        consistency=consistency,
        citation=citation,
        robustness=robustness,
    )


async def run_eval(
    *,
    dimensions: Sequence[Dimension],
    output_path: Path,
) -> EvalReport:
    """Run all dimensions concurrently and write the report JSON.

    Args:
        dimensions: Concrete ``Dimension`` instances to evaluate. Order does
            not affect the report; runs are gathered concurrently.
        output_path: Destination for the JSON-serialized ``EvalReport``.
            Parent directory must exist.

    Returns:
        The assembled ``EvalReport``.
    """
    log.info(
        "eval.harness.start",
        component="eval_harness",
        num_dimensions=len(dimensions),
        dimensions=[d.kind.value for d in dimensions],
    )

    runs = await asyncio.gather(*(d.evaluate() for d in dimensions))
    report = assemble_report(run_id=str(uuid.uuid4()), runs=list(runs))

    output_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    log.info(
        "eval.harness.done",
        component="eval_harness",
        run_id=report.run_id,
        overall_passed=report.overall_passed,
        output_path=str(output_path),
        dimensions_failed=[
            d.dimension.value for d in report.dimensions if not d.passed
        ],
    )

    return report


_DEFAULT_DATASET_ROOT = Path("eval/datasets")


def _build_default_dimensions(
    *,
    settings: Settings | None = None,
    dataset_root: Path | None = None,
) -> list[Dimension]:
    """Construct the MVP dimension set against live infrastructure.

    Each dimension's golden dataset is loaded from
    ``dataset_root/<dimension>/...``. When a dataset file is missing
    (typically because curation happens in MVP plan Step 4), the slot
    is filled with a ``SkippedDimension`` so the ``EvalReport`` still
    lists all five dimensions and the missing one surfaces as a
    threshold violation instead of disappearing silently.

    Args:
        settings: Optional ``Settings`` override. Defaults to a fresh
            ``Settings()`` reading from the environment. Tests pass a
            stub Settings to drive the API-key fail-fast path.
        dataset_root: Optional override for the dataset directory.
            Defaults to ``eval/datasets/`` relative to the working
            directory.

    Returns:
        Five dimensions in canonical order: retrieval, faithfulness,
        consistency, citation, robustness. Each is either a real
        dimension (dataset present) or a ``SkippedDimension`` (dataset
        missing).

    Raises:
        ValueError: ``OPENAI_API_KEY`` is not set in ``settings``.
    """
    settings = settings if settings is not None else Settings()
    if not settings.openai_api_key:
        msg = (
            "OPENAI_API_KEY is required to construct the eval harness. "
            "Set OPENAI_API_KEY in the environment or in .env."
        )
        raise ValueError(msg)

    dataset_root = dataset_root if dataset_root is not None else _DEFAULT_DATASET_ROOT

    # Heavy clients — opened once, shared across dimensions.
    driver = PipelineDriver(settings=settings)
    judge_client = OpenAIJudgeClient(client=AsyncOpenAI())
    ragas_scorer = RagasFaithfulnessAdapter()

    dimensions: list[Dimension] = []

    # --- Retrieval ---
    queries_path = dataset_root / "retrieval" / "golden_queries.yaml"
    if queries_path.exists():
        dimensions.append(
            RetrievalDimension(
                retriever=driver.retriever,
                golden_queries=load_golden_queries(queries_path),
            )
        )
    else:
        dimensions.append(
            SkippedDimension(
                kind=EvalDimension.RETRIEVAL,
                reason=f"dataset missing: {queries_path}",
            )
        )

    # --- Faithfulness ---
    faithfulness_path = dataset_root / "faithfulness" / "golden_outputs.yaml"
    if faithfulness_path.exists():
        dimensions.append(
            FaithfulnessDimension(
                ragas_scorer=ragas_scorer,
                judge_client=judge_client,
                cases=load_faithfulness_cases(faithfulness_path),
            )
        )
    else:
        dimensions.append(
            SkippedDimension(
                kind=EvalDimension.FAITHFULNESS,
                reason=f"dataset missing: {faithfulness_path}",
            )
        )

    # --- Consistency ---
    scenarios_dir = dataset_root / "scenarios"
    scenario_files = (
        sorted(scenarios_dir.glob("*.yaml")) if scenarios_dir.exists() else []
    )
    if scenario_files:
        scenarios = []
        for f in scenario_files:
            scenarios.extend(load_scenarios(f))
        dimensions.append(ConsistencyDimension(driver=driver, scenarios=scenarios))
    else:
        dimensions.append(
            SkippedDimension(
                kind=EvalDimension.CONSISTENCY,
                reason=f"no scenario files in {scenarios_dir}/",
            )
        )

    # --- Citation ---
    citation_path = dataset_root / "citations" / "golden_outputs.yaml"
    if citation_path.exists():
        dimensions.append(
            CitationDimension(
                client=judge_client,
                cases=load_citation_cases(citation_path),
            )
        )
    else:
        dimensions.append(
            SkippedDimension(
                kind=EvalDimension.CITATION,
                reason=f"dataset missing: {citation_path}",
            )
        )

    # --- Robustness ---
    # Each sub-check has its own dataset file. RobustnessDimension accepts
    # any subset; we pass each list only when its file exists. If all four
    # are missing, fall back to SkippedDimension.
    robustness_dir = dataset_root / "robustness"
    injection_path = robustness_dir / "injection.yaml"
    schema_path = robustness_dir / "malformed.yaml"
    novel_path = robustness_dir / "novel.yaml"
    fallback_path = robustness_dir / "fallback.yaml"
    sub_paths = [injection_path, schema_path, novel_path, fallback_path]
    if any(p.exists() for p in sub_paths):
        dimensions.append(
            RobustnessDimension(
                driver=driver,
                injection_cases=(
                    load_injection_cases(injection_path)
                    if injection_path.exists()
                    else None
                ),
                schema_cases=(
                    load_schema_violation_cases(schema_path)
                    if schema_path.exists()
                    else None
                ),
                novel_cases=(
                    load_novel_pattern_cases(novel_path)
                    if novel_path.exists()
                    else None
                ),
                fallback_cases=(
                    load_fallback_cases(fallback_path)
                    if fallback_path.exists()
                    else None
                ),
            )
        )
    else:
        dimensions.append(
            SkippedDimension(
                kind=EvalDimension.ROBUSTNESS,
                reason=f"all sub-check datasets missing in {robustness_dir}/",
            )
        )

    log.info(
        "eval.harness.dimensions_built",
        component="eval_harness",
        num_dimensions=len(dimensions),
        skipped=[d.kind.value for d in dimensions if isinstance(d, SkippedDimension)],
    )
    return dimensions


_DEFAULT_OUTPUT_PATH = Path("outputs/stage/eval/eval-report.json")


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for ``make eval``.

    Resolves the output path (in precedence order: ``--output`` flag,
    ``EVAL_OUTPUT_PATH`` env var, then the default staging path),
    constructs the default dimension set, and runs the harness.

    Exit codes:
        0: all dimensions passed thresholds (``overall_passed=True``).
        1: one or more dimensions failed thresholds.
        2: config error — no dimensions registered. No report written.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).
            Provided for testability.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(
        prog="eval.runners.harness",
        description="Run the 5-dimension evaluation harness.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Path to write the EvalReport JSON. Defaults to "
            "$EVAL_OUTPUT_PATH or outputs/stage/eval/eval-report.json."
        ),
    )
    args = parser.parse_args(argv)

    output_path: Path = args.output or Path(
        os.environ.get("EVAL_OUTPUT_PATH", str(_DEFAULT_OUTPUT_PATH))
    )

    try:
        dimensions = _build_default_dimensions()
    except ValueError as exc:
        # Configuration errors (missing env vars, etc.) — print clean
        # message; exit 2 distinguishes config failure from threshold
        # failure (exit 1).
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not dimensions:
        print(
            "error: no dimensions registered; eval harness has nothing to "
            "evaluate. See eval/README.md for the dimension wiring story "
            "(MVP plan Step 3).",
            file=sys.stderr,
        )
        return 2

    output_path.parent.mkdir(parents=True, exist_ok=True)
    report = asyncio.run(run_eval(dimensions=dimensions, output_path=output_path))
    return 0 if report.overall_passed else 1


if __name__ == "__main__":
    sys.exit(main())
