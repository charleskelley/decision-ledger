"""The daily mock's ``EvalAdapter`` for the framework eval harness.

Published as the ``mock-daily-reasoner`` entry point in the
``decisionledger.eval_adapters`` group, so ``uv run python -m
eval.runners.harness --reasoner-id mock-daily-reasoner`` (``make
eval-mock_daily``, one of ``make eval-mocks``) resolves it: the row type its
datasets are written in, the datasets, and an in-process driver into its
pipeline. The mock ships only ``scenarios/`` and ``robustness/`` datasets,
so the RAG policy gate's own eval never selects it.

Importing this module reads no settings and opens nothing: the harness, the
policy gate's eval, and the corpus loader load every installed adapter or
plugin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from mock_daily_reasoner.registry import MOCK_DAILY_REGISTRATION
from mock_daily_reasoner.settlements import AccountSettlement

if TYPE_CHECKING:
    from app.settings import FrameworkSettings
    from mock_daily_reasoner.eval.driver import MockDailyEvalDriver

_DATASETS_DIR = Path(__file__).parent / "datasets"


@dataclass(frozen=True)
class MockDailyEvalAdapter:
    """Supplies the daily mock's row type, datasets, and pipeline driver.

    Attributes:
        reasoner_id: ``mock-daily-reasoner``.
        event_type: ``AccountSettlement``.
        datasets_dir: ``scenarios/`` (consistency across the order of a
            day's rows) and ``robustness/`` (schema failure and fallback;
            both deterministic).
    """

    reasoner_id: str = MOCK_DAILY_REGISTRATION.reasoner_id
    event_type: type[AccountSettlement] = AccountSettlement
    datasets_dir: Path = field(default=_DATASETS_DIR)

    def build_driver(self, settings: FrameworkSettings) -> MockDailyEvalDriver:
        """Open the mock's pipeline against the ledger, in process.

        Args:
            settings: Framework settings (Postgres).

        Returns:
            A connected driver over the mock's plugin and the rules gate
            (with the member's own rule set); the caller closes it.
        """
        from mock_daily_reasoner.eval.driver import MockDailyEvalDriver

        return MockDailyEvalDriver(settings)


#: The adapter instance the entry point names.
ADAPTER = MockDailyEvalAdapter()
