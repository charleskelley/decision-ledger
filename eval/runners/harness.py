"""Eval harness runner.

Orchestrates a list of ``Dimension`` implementations into a single
``EvalReport``. Writes the report as JSON, exits non-zero on any threshold
violation — that's the merge gate.

Dimensions are constructed and passed in by the caller (CLI entrypoint or
test harness). The runner does not know which dimensions exist; it only
asks each one to ``evaluate()`` and assembles the report. This keeps the
runner stable as new dimensions are added.

The CLI evaluates one reasoner at a time. ``--reasoner-id`` (or
``DECISIONLEDGER_EVAL_REASONER``) names it; the reasoner's ``EvalAdapter``
(see ``eval.adapter``) supplies the event type, golden datasets, and the
pipeline driver. The harness itself carries no reasoner vocabulary.

The framework's dimensions are the reasoner-facing ones: decision
consistency and adversarial robustness, driven through the reasoner's own
pipeline, and shadow evaluation, which re-decides a sample of the decisions
the run recorded with the reasoner's advisory gates (report-only; it runs
after the others because it samples what they record). Dimensions that
measure what one gate does (a retrieving gate's retrieval quality, an LLM
gate's faithfulness and citations) belong to that gate and run in its own
harness, which reuses ``run_eval`` and the ``EvalReport`` schema (DR-28).

Reports are staged per reasoner at
``outputs/stage/eval/reasoner/<reasoner_id>/eval-report.json``; promote a
baseline by copying it to
``outputs/eval/reasoner/<reasoner_id>/eval-report-v<N>.json``.

Usage::

    uv run python -m eval.runners.harness --reasoner-id <reasoner-id>

    # or programmatically
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

from app.settings import FrameworkSettings
from core.eval.metrics import (
    CitationMetrics,
    ConsistencyMetrics,
    EvalDimension,
    EvalReport,
    FaithfulnessMetrics,
    RetrievalMetrics,
    RobustnessMetrics,
    ShadowMetrics,
)
from core.exceptions import ReasonerPluginError
from eval.adapter import load_eval_adapter
from eval.dimensions.consistency import ConsistencyDimension, load_scenarios
from eval.dimensions.robustness import (
    RobustnessDimension,
    load_fallback_cases,
    load_injection_cases,
    load_novel_pattern_cases,
    load_schema_violation_cases,
)
from eval.dimensions.shadow import ShadowDimension
from eval.dimensions.skipped import SkippedDimension

if TYPE_CHECKING:
    from collections.abc import Sequence

    from eval.adapter import EvalAdapter
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
    shadow: ShadowMetrics | None = None

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
            case ShadowMetrics():
                shadow = run.metrics
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
        shadow=shadow,
    )


async def run_eval(
    *,
    dimensions: Sequence[Dimension],
    output_path: Path,
) -> EvalReport:
    """Run all dimensions concurrently and write the report JSON.

    Dimensions that sample what the others record (a ``runs_after_others``
    attribute set true, like the shadow dimension) run in a second wave,
    once the first has finished.

    Args:
        dimensions: Concrete ``Dimension`` instances to evaluate. Within a
            wave, order does not affect the report; runs are gathered
            concurrently.
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

    first = [d for d in dimensions if not getattr(d, "runs_after_others", False)]
    later = [d for d in dimensions if getattr(d, "runs_after_others", False)]
    runs = list(await asyncio.gather(*(d.evaluate() for d in first)))
    runs += await asyncio.gather(*(d.evaluate() for d in later))
    report = assemble_report(run_id=str(uuid.uuid4()), runs=runs)

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


#: Env var naming the reasoner to evaluate when ``--reasoner-id`` is absent.
_REASONER_ENV = "DECISIONLEDGER_EVAL_REASONER"


def _build_default_dimensions(
    *,
    adapter: EvalAdapter,
    settings: FrameworkSettings | None = None,
    dataset_root: Path | None = None,
) -> list[Dimension]:
    """Construct the framework's dimensions for one reasoner on live infrastructure.

    Each dimension's golden dataset is loaded from
    ``dataset_root/<dimension>/...``. When a dataset is missing the slot is
    filled with a ``SkippedDimension`` so the ``EvalReport`` still lists both
    dimensions and the missing one surfaces as a threshold violation instead
    of disappearing silently.

    Gate-owned dimensions are not built here: each gate's own harness runs
    them against the datasets a reasoner ships for that gate. The shadow
    dimension needs no dataset: it samples the decisions this run records
    (``since`` = now) through the driver's framework services.

    Args:
        adapter: The reasoner's ``EvalAdapter``. Supplies the event type the
            consistency and robustness datasets are validated against, the
            default dataset root, and the pipeline driver.
        settings: Optional ``FrameworkSettings`` override handed to the
            adapter's driver. Defaults to a fresh ``FrameworkSettings()``
            reading from the environment.
        dataset_root: Optional override for the dataset directory.
            Defaults to ``adapter.datasets_dir``.

    Returns:
        Three dimensions in canonical order: consistency, robustness, shadow.
        The first two are each either a real dimension (dataset present) or
        a ``SkippedDimension`` (dataset missing).

    Raises:
        ValueError: The adapter's driver rejects its configuration (for
            example a missing credential the reasoner's pipeline needs).
    """
    settings = settings if settings is not None else FrameworkSettings()
    dataset_root = dataset_root if dataset_root is not None else adapter.datasets_dir
    event_type = adapter.event_type
    started = datetime.now(UTC)

    # Opened once, shared by both dimensions.
    driver = adapter.build_driver(settings)

    dimensions: list[Dimension] = []

    # --- Consistency ---
    scenarios_dir = dataset_root / "scenarios"
    scenario_files = (
        sorted(scenarios_dir.glob("*.yaml")) if scenarios_dir.exists() else []
    )
    if scenario_files:
        scenarios = []
        for f in scenario_files:
            scenarios.extend(load_scenarios(f, event_type=event_type))
        dimensions.append(ConsistencyDimension(driver=driver, scenarios=scenarios))
    else:
        dimensions.append(
            SkippedDimension(
                kind=EvalDimension.CONSISTENCY,
                reason=f"no scenario files in {scenarios_dir}/",
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
                    load_injection_cases(injection_path, event_type=event_type)
                    if injection_path.exists()
                    else None
                ),
                schema_cases=(
                    load_schema_violation_cases(schema_path, event_type=event_type)
                    if schema_path.exists()
                    else None
                ),
                novel_cases=(
                    load_novel_pattern_cases(novel_path, event_type=event_type)
                    if novel_path.exists()
                    else None
                ),
                fallback_cases=(
                    load_fallback_cases(fallback_path, event_type=event_type)
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

    # --- Shadow (report-only; samples what the dimensions above record) ---
    framework = driver.framework
    dimensions.append(
        ShadowDimension(
            source=framework.store,
            gates=framework.gates,
            registry=framework.registry,
            reasoner_id=adapter.reasoner_id,
            since=started,
        )
    )

    log.info(
        "eval.harness.dimensions_built",
        component="eval_harness",
        reasoner_id=adapter.reasoner_id,
        num_dimensions=len(dimensions),
        skipped=[d.kind.value for d in dimensions if isinstance(d, SkippedDimension)],
    )
    return dimensions


#: Staging directory for reasoner reports: ``<this>/<reasoner_id>/eval-report.json``.
_DEFAULT_OUTPUT_DIR = Path("outputs/stage/eval/reasoner")


def default_output_path(reasoner_id: str) -> Path:
    """Staging path of one reasoner's report (``outputs/eval/<kind>/<id>`` layout)."""
    return _DEFAULT_OUTPUT_DIR / reasoner_id / "eval-report.json"


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for ``make eval``.

    Resolves the reasoner to evaluate (``--reasoner-id`` flag, else the
    ``DECISIONLEDGER_EVAL_REASONER`` env var) and loads its ``EvalAdapter``;
    resolves the output path (in precedence order: ``--output`` flag,
    ``EVAL_OUTPUT_PATH`` env var, then the per-reasoner staging path
    ``outputs/stage/eval/reasoner/<reasoner_id>/eval-report.json``);
    constructs the default dimension set; and runs the harness.

    Exit codes:
        0: all dimensions passed thresholds (``overall_passed=True``).
        1: one or more dimensions failed thresholds.
        2: config error — no reasoner named, no eval adapter for it, a
            driver configuration error, or no dimensions registered. No
            report written.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).
            Provided for testability.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(
        prog="eval.runners.harness",
        description=(
            "Run the framework's evaluation harness (consistency, robustness, "
            "shadow) for one reasoner."
        ),
    )
    parser.add_argument(
        "--reasoner-id",
        default=None,
        help=(
            "reasoner_id whose EvalAdapter supplies the datasets and pipeline "
            f"driver. Defaults to ${_REASONER_ENV}; one of the two is required."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Path to write the EvalReport JSON. Defaults to $EVAL_OUTPUT_PATH "
            "or outputs/stage/eval/reasoner/<reasoner_id>/eval-report.json."
        ),
    )
    args = parser.parse_args(argv)

    reasoner_id = args.reasoner_id or os.environ.get(_REASONER_ENV, "").strip()
    if not reasoner_id:
        print(
            f"error: no reasoner to evaluate; pass --reasoner-id or set "
            f"{_REASONER_ENV}.",
            file=sys.stderr,
        )
        return 2

    output_path: Path = args.output or Path(
        os.environ.get("EVAL_OUTPUT_PATH", str(default_output_path(reasoner_id)))
    )

    try:
        adapter = load_eval_adapter(reasoner_id)
    except ReasonerPluginError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        dimensions = _build_default_dimensions(adapter=adapter)
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
