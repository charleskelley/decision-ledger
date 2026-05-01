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

from core.eval.metrics import (
    CitationMetrics,
    ConsistencyMetrics,
    EvalReport,
    FaithfulnessMetrics,
    RetrievalMetrics,
    RobustnessMetrics,
)

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


def _build_default_dimensions() -> list[Dimension]:
    """Construct the MVP dimension set.

    Returns an empty list while concrete dimensions are still being
    implemented — the harness shell is callable end-to-end with stubs.
    Real implementations replace this as each dimension lands.
    """
    return []


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

    dimensions = _build_default_dimensions()
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
