"""The policy gate's eval harness: its three dimensions over reasoners' datasets.

The framework harness (``eval.runners.harness``) runs the reasoner-facing
dimensions — decision consistency and adversarial robustness — through a
reasoner's own pipeline. This harness runs the dimensions this gate owns
(DR-28) against the golden datasets a reasoner ships for the gate, read from
the reasoner's ``EvalAdapter.datasets_dir``:

    retrieval/golden_queries.yaml     RetrievalDimension (the gate's own
                                      hybrid retriever, tenant-filtered)
    faithfulness/golden_outputs.yaml  FaithfulnessDimension (RAGAS + judge)
    citations/golden_outputs.yaml     CitationDimension (LLM judges)

A missing dataset becomes a ``SkippedDimension``, so the report still lists
all three and the gap counts as a failure. Reports are the framework's
``EvalReport`` written by its ``run_eval``, one per reasoner, at
``<output-dir>/<reasoner_id>/eval-report.json`` (default output dir
``outputs/stage/eval/gate/policy``). Promote a baseline by copying it to
``outputs/eval/gate/policy/<reasoner_id>/eval-report-v<N>.json``.

Usage::

    # every installed reasoner whose eval adapter ships datasets for this gate
    uv run python -m policy_rag_gate.eval
    # one reasoner (or set DECISIONLEDGER_EVAL_REASONER)
    uv run python -m policy_rag_gate.eval --reasoner-id <reasoner-id>

The judges (Anthropic) and RAGAS (OpenAI) make paid LLM calls; the retrieval
dimension needs the gate's stores with the reasoner's corpus indexed.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata as metadata
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from app.settings import FrameworkSettings
from core.eval.metrics import EvalDimension
from core.exceptions import ReasonerPluginError
from eval.adapter import EVAL_ADAPTER_GROUP, load_eval_adapter
from eval.dimensions.skipped import SkippedDimension
from eval.runners.harness import run_eval
from policy_rag_gate.eval.citation import CitationDimension, load_citation_cases
from policy_rag_gate.eval.faithfulness import (
    FaithfulnessDimension,
    load_faithfulness_cases,
)
from policy_rag_gate.eval.ragas import RagasFaithfulnessAdapter
from policy_rag_gate.eval.retrieval import RetrievalDimension, load_golden_queries
from policy_rag_gate.gate import POLICY_GATE_ID
from policy_rag_gate.llm.anthropic import AnthropicLLMClient
from policy_rag_gate.plugin import open_retriever
from policy_rag_gate.settings import PolicyRagGateSettings

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from importlib.metadata import EntryPoint

    from core.eval.metrics import EvalReport
    from core.llm import LLMClient
    from eval.adapter import EvalAdapter
    from eval.dimensions import Dimension
    from policy_rag_gate.eval.faithfulness import RagasFaithfulnessScorer
    from policy_rag_gate.eval.retrieval import RetrieverClient

log = structlog.get_logger(__name__)

#: Golden datasets, relative to a reasoner's ``datasets_dir``.
RETRIEVAL_DATASET = Path("retrieval", "golden_queries.yaml")
FAITHFULNESS_DATASET = Path("faithfulness", "golden_outputs.yaml")
CITATION_DATASET = Path("citations", "golden_outputs.yaml")
GATE_DATASETS: tuple[Path, ...] = (
    RETRIEVAL_DATASET,
    FAITHFULNESS_DATASET,
    CITATION_DATASET,
)

#: Where reports are staged: ``<this>/<reasoner_id>/eval-report.json``.
DEFAULT_OUTPUT_DIR = Path("outputs", "stage", "eval", "gate", POLICY_GATE_ID)
REPORT_NAME = "eval-report.json"

#: Env var naming the one reasoner to evaluate when ``--reasoner-id`` is absent.
_REASONER_ENV = "DECISIONLEDGER_EVAL_REASONER"


def ships_gate_datasets(datasets_dir: Path) -> bool:
    """Return True when ``datasets_dir`` holds any of this gate's datasets."""
    return any((datasets_dir / dataset).is_file() for dataset in GATE_DATASETS)


def select_adapters(
    reasoner_id: str | None,
    *,
    entry_points: Iterable[EntryPoint] | None = None,
) -> list[EvalAdapter]:
    """Resolve the reasoner eval adapters to evaluate the gate against.

    Args:
        reasoner_id: One reasoner to evaluate, or ``None`` for every
            installed adapter whose ``datasets_dir`` ships at least one of
            this gate's datasets.
        entry_points: Explicit ``decisionledger.eval_adapters`` entry points
            instead of the installed ones (tests).

    Returns:
        The adapters, sorted by ``reasoner_id``. Empty when nothing installed
        ships datasets for this gate.

    Raises:
        ReasonerPluginError: ``reasoner_id`` names no installed adapter, or
            an adapter fails to load.
    """
    eps = (
        list(entry_points)
        if entry_points is not None
        else list(metadata.entry_points(group=EVAL_ADAPTER_GROUP))
    )
    if reasoner_id is not None:
        return [load_eval_adapter(reasoner_id, entry_points=eps)]
    adapters = [
        load_eval_adapter(name, entry_points=eps)
        for name in sorted({ep.name for ep in eps})
    ]
    return [a for a in adapters if ships_gate_datasets(a.datasets_dir)]


def build_gate_dimensions(
    *,
    reasoner_id: str,
    dataset_root: Path,
    retriever: RetrieverClient,
    judge_client: LLMClient,
    ragas_scorer: RagasFaithfulnessScorer,
) -> list[Dimension]:
    """Build the gate's three dimensions over one reasoner's datasets.

    Args:
        reasoner_id: Tenant the retrieval dimension filters on.
        dataset_root: The reasoner's ``datasets_dir``.
        retriever: The gate's retriever (a stub in unit tests).
        judge_client: ``LLMClient`` for the faithfulness and citation judges.
        ragas_scorer: RAGAS faithfulness scorer.

    Returns:
        Retrieval, faithfulness, and citation — each a real dimension when
        its dataset exists, else a ``SkippedDimension`` naming the path.
    """
    dimensions: list[Dimension] = []

    queries_path = dataset_root / RETRIEVAL_DATASET
    if queries_path.exists():
        dimensions.append(
            RetrievalDimension(
                retriever=retriever,
                golden_queries=load_golden_queries(queries_path),
                reasoner_id=reasoner_id,
            )
        )
    else:
        dimensions.append(
            SkippedDimension(
                kind=EvalDimension.RETRIEVAL,
                reason=f"dataset missing: {queries_path}",
            )
        )

    faithfulness_path = dataset_root / FAITHFULNESS_DATASET
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

    citation_path = dataset_root / CITATION_DATASET
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

    log.info(
        "policy_gate.eval.dimensions_built",
        component="policy_gate_eval",
        reasoner_id=reasoner_id,
        skipped=[d.kind.value for d in dimensions if isinstance(d, SkippedDimension)],
    )
    return dimensions


async def evaluate_reasoners(
    *,
    adapters: Sequence[EvalAdapter],
    retriever: RetrieverClient,
    judge_client: LLMClient,
    ragas_scorer: RagasFaithfulnessScorer,
    output_dir: Path,
) -> list[EvalReport]:
    """Run the gate's dimensions for each reasoner and write one report each.

    One event loop serves every reasoner, so the LLM clients' connection
    pools are never carried across loops.

    Returns:
        The reports, in ``adapters`` order.
    """
    reports: list[EvalReport] = []
    for adapter in adapters:
        dimensions = build_gate_dimensions(
            reasoner_id=adapter.reasoner_id,
            dataset_root=adapter.datasets_dir,
            retriever=retriever,
            judge_client=judge_client,
            ragas_scorer=ragas_scorer,
        )
        output_path = output_dir / adapter.reasoner_id / REPORT_NAME
        output_path.parent.mkdir(parents=True, exist_ok=True)
        reports.append(await run_eval(dimensions=dimensions, output_path=output_path))
    return reports


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for ``make eval-gate-policy_rag``.

    Exit codes:
        0: every report passed its thresholds.
        1: a dimension failed its thresholds (or was skipped).
        2: config error — no reasoner to evaluate, an unknown reasoner, or a
            missing API key. No report written.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(
        prog="policy_rag_gate.eval",
        description=(
            "Run the policy gate's eval (retrieval, faithfulness, citation) "
            "against reasoners' golden datasets."
        ),
    )
    parser.add_argument(
        "--reasoner-id",
        default=None,
        help=(
            "Evaluate against this reasoner's datasets only. Defaults to "
            f"${_REASONER_ENV}, else every installed reasoner eval adapter that "
            "ships datasets for this gate."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=f"Report directory (default {DEFAULT_OUTPUT_DIR}/<reasoner_id>/).",
    )
    args = parser.parse_args(argv)
    reasoner_id = args.reasoner_id or os.environ.get(_REASONER_ENV, "").strip() or None
    output_dir: Path = args.output_dir or DEFAULT_OUTPUT_DIR

    try:
        adapters = select_adapters(reasoner_id)
    except ReasonerPluginError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not adapters:
        print(
            "error: no installed reasoner eval adapter ships datasets for this "
            f"gate ({', '.join(str(d) for d in GATE_DATASETS)}); pass "
            "--reasoner-id to evaluate one anyway.",
            file=sys.stderr,
        )
        return 2

    settings = PolicyRagGateSettings()
    missing = [
        name
        for name, value in (
            ("OPENAI_API_KEY", settings.openai_api_key),
            ("ANTHROPIC_API_KEY", settings.anthropic_api_key),
        )
        if not value
    ]
    if missing:
        print(
            f"error: {' and '.join(missing)} required: RAGAS scores faithfulness "
            "on OpenAI and the judges run on Anthropic (cross-family, to reduce "
            "in-family bias against the OpenAI-backed gate). Set them in the "
            "environment or in .env.",
            file=sys.stderr,
        )
        return 2

    retriever, resources = open_retriever(
        postgres_dsn=FrameworkSettings().postgres_dsn,
        elasticsearch_url=settings.elasticsearch_url,
    )
    try:
        reports = asyncio.run(
            evaluate_reasoners(
                adapters=adapters,
                retriever=retriever,
                judge_client=AnthropicLLMClient(),
                ragas_scorer=RagasFaithfulnessAdapter(),
                output_dir=output_dir,
            )
        )
    finally:
        for resource in resources:
            resource.close()
    return 0 if all(report.overall_passed for report in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
