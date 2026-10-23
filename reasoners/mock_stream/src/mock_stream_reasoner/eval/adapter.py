"""The stream mock's ``EvalAdapter`` for the framework eval harness.

Published as the ``mock-stream-reasoner`` entry point in the
``decisionledger.eval_adapters`` group, so ``uv run python -m
eval.runners.harness --reasoner-id mock-stream-reasoner`` (``make
eval-mock_stream``, one of ``make eval-mocks``) resolves it: the event type
its datasets are written in, the datasets, and an in-process driver into its
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

from mock_stream_reasoner.events import TransferEvent
from mock_stream_reasoner.registry import MOCK_STREAM_REGISTRATION

if TYPE_CHECKING:
    from app.settings import FrameworkSettings
    from mock_stream_reasoner.eval.driver import MockStreamEvalDriver

_DATASETS_DIR = Path(__file__).parent / "datasets"


@dataclass(frozen=True)
class MockStreamEvalAdapter:
    """Supplies the stream mock's event type, datasets, and pipeline driver.

    Attributes:
        reasoner_id: ``mock-stream-reasoner``.
        event_type: ``TransferEvent``.
        datasets_dir: ``scenarios/`` (consistency across precursor orderings
            of stateful features) and ``robustness/`` (schema failure and
            fallback; both deterministic).
    """

    reasoner_id: str = MOCK_STREAM_REGISTRATION.reasoner_id
    event_type: type[TransferEvent] = TransferEvent
    datasets_dir: Path = field(default=_DATASETS_DIR)

    def build_driver(self, settings: FrameworkSettings) -> MockStreamEvalDriver:
        """Open the mock's pipeline against the ledger, in process.

        Args:
            settings: Framework settings (Postgres).

        Returns:
            A connected driver over the mock's plugin and its scripted
            policy double; the caller closes it.
        """
        from mock_stream_reasoner.eval.driver import MockStreamEvalDriver

        return MockStreamEvalDriver(settings)


#: The adapter instance the entry point names.
ADAPTER = MockStreamEvalAdapter()
