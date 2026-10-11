"""Reasoner-supplied eval adapters, discovered via Python entry points.

The eval harness is reasoner-agnostic: it owns the five dimensions and the
report format, but every reasoner-specific piece (its event type, its golden
datasets, and an in-process driver into its decision pipeline) comes from an
``EvalAdapter`` the reasoner publishes under the ``decisionledger.eval_adapters``
entry point group. The entry-point name must equal the adapter's
``reasoner_id``; ``load_eval_adapter`` resolves one adapter by that id.

This group is separate from the runtime ``decisionledger.reasoners`` group so
``app/`` never has to import anything from ``eval/``.
"""

from __future__ import annotations

import importlib.metadata as metadata
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from core.exceptions import ReasonerPluginError

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from importlib.metadata import EntryPoint
    from pathlib import Path

    from pydantic import BaseModel

    from app.settings import FrameworkSettings
    from core.actions import DecisionAction
    from eval.dimensions.consistency import PipelineRunResult
    from eval.dimensions.retrieval import RetrieverClient

log = structlog.get_logger(__name__)

#: Entry point group every reasoner eval adapter registers under.
EVAL_ADAPTER_GROUP = "decisionledger.eval_adapters"


class EvalDriver(Protocol):
    """In-process driver into one reasoner's live decision pipeline.

    One driver backs the retrieval, consistency, and robustness dimensions,
    so it satisfies the consistency ``PipelineDriver`` and robustness
    ``RobustnessDriver`` protocols structurally and also exposes the
    retriever it runs on. Events are instances of the adapter's
    ``event_type``; the harness never inspects them.

    Attributes:
        retriever: The policy retriever the pipeline uses, reused by the
            retrieval dimension so eval opens one set of connections.
    """

    retriever: RetrieverClient

    async def run(self, events: Sequence[Any]) -> PipelineRunResult:
        """Run an ordered event sequence; return the trigger's result."""
        ...

    async def run_event(self, event: Any) -> DecisionAction:
        """Run one event through the normal pipeline; return the action."""
        ...

    async def run_with_forced_schema_failure(self, event: Any) -> DecisionAction:
        """Force a missing gate verdict; return enforcement's action."""
        ...

    async def run_with_forced_fallback(
        self, event: Any, *, fallback_kind: str
    ) -> DecisionAction:
        """Force one fallback path (retrieval, corpus, or LLM failure)."""
        ...

    def reset_state(self) -> None:
        """Discard the reasoner's accumulated per-entity state between cases."""
        ...

    def close(self) -> None:
        """Release every connection the driver holds."""
        ...


@runtime_checkable
class EvalAdapter(Protocol):
    """What a reasoner hands the eval harness.

    Attributes:
        reasoner_id: Tenant id the adapter evaluates. Must equal the entry
            point name and the reasoner's ``registration.reasoner_id``.
        event_type: Pydantic model the reasoner's datasets are written in.
            The consistency and robustness loaders validate events against it.
        datasets_dir: Root of the reasoner's golden datasets, laid out as
            ``retrieval/``, ``faithfulness/``, ``scenarios/``, ``citations/``,
            and ``robustness/``.
    """

    reasoner_id: str
    event_type: type[BaseModel]
    datasets_dir: Path

    def build_driver(self, settings: FrameworkSettings) -> EvalDriver:
        """Open the reasoner's pipeline against live infrastructure.

        Args:
            settings: Framework settings (database, search, LLM keys).

        Returns:
            A connected ``EvalDriver``. The caller closes it.
        """
        ...


def load_eval_adapter(
    reasoner_id: str,
    *,
    entry_points: Iterable[EntryPoint] | None = None,
) -> EvalAdapter:
    """Resolve the eval adapter a reasoner publishes for ``reasoner_id``.

    Args:
        reasoner_id: Tenant id to evaluate (the entry-point name).
        entry_points: Explicit entry points to search instead of querying
            ``importlib.metadata`` for ``EVAL_ADAPTER_GROUP``. Tests use this
            to inject in-module fakes.

    Returns:
        The loaded adapter.

    Raises:
        ReasonerPluginError: No entry point is named ``reasoner_id`` (the
            message lists the ids that are installed), the entry point fails
            to load, the object does not satisfy ``EvalAdapter``, or its
            ``reasoner_id`` differs from the entry-point name.
    """
    eps = (
        list(entry_points)
        if entry_points is not None
        else list(metadata.entry_points(group=EVAL_ADAPTER_GROUP))
    )
    matches = [ep for ep in eps if ep.name == reasoner_id]
    if not matches:
        available = sorted({ep.name for ep in eps})
        raise ReasonerPluginError(
            reasoner_id,
            f"no eval adapter registered under {EVAL_ADAPTER_GROUP!r}; "
            f"installed adapters: {available or 'none'}",
        )
    if len(matches) > 1:
        raise ReasonerPluginError(
            reasoner_id,
            "multiple eval adapters share this id: "
            f"{sorted(ep.value for ep in matches)}",
        )
    ep = matches[0]

    try:
        obj = ep.load()
    except Exception as err:
        raise ReasonerPluginError(
            reasoner_id, f"entry point {ep.value!r} failed to load: {err}"
        ) from err

    if not isinstance(obj, EvalAdapter):
        raise ReasonerPluginError(
            reasoner_id,
            f"object {obj!r} loaded from {ep.value!r} does not satisfy the "
            "EvalAdapter protocol (reasoner_id, event_type, datasets_dir, "
            "build_driver)",
        )
    if obj.reasoner_id != reasoner_id:
        raise ReasonerPluginError(
            reasoner_id,
            f"entry point name must equal the adapter's reasoner_id "
            f"({obj.reasoner_id!r})",
        )

    log.info(
        "eval.adapter.loaded",
        component="eval_adapter",
        reasoner_id=reasoner_id,
        entry_point=ep.value,
    )
    return obj
