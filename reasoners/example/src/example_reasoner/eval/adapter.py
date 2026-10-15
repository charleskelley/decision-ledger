"""The example reasoner's ``EvalAdapter`` for the framework eval harness.

Published as the ``example-reasoner`` entry point in the
``decisionledger.eval_adapters`` group, so ``uv run python -m
eval.runners.harness --reasoner-id example-reasoner`` (``make eval-example``)
resolves it: the event type its datasets are written in, the datasets, and
an in-process driver into its pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from example_reasoner.events import WidgetEvent
from example_reasoner.registry import EXAMPLE_REGISTRATION

if TYPE_CHECKING:
    from app.settings import FrameworkSettings
    from example_reasoner.eval.driver import ExampleEvalDriver

_DATASETS_DIR = Path(__file__).parent / "datasets"


@dataclass(frozen=True)
class ExampleEvalAdapter:
    """Supplies the example's event type, datasets, and pipeline driver.

    Attributes:
        reasoner_id: ``example-reasoner``.
        event_type: ``WidgetEvent``.
        datasets_dir: ``scenarios/`` (consistency) and ``robustness/``
            (schema failure and fallback; both deterministic).
    """

    reasoner_id: str = EXAMPLE_REGISTRATION.reasoner_id
    event_type: type[WidgetEvent] = WidgetEvent
    datasets_dir: Path = field(default=_DATASETS_DIR)

    def build_driver(self, settings: FrameworkSettings) -> ExampleEvalDriver:
        """Open the example pipeline against the ledger, in process.

        Args:
            settings: Framework settings (Postgres).

        Returns:
            A connected driver; the caller closes it.
        """
        from example_reasoner.eval.driver import ExampleEvalDriver

        return ExampleEvalDriver(settings)


#: The adapter instance the entry point names.
ADAPTER = ExampleEvalAdapter()
