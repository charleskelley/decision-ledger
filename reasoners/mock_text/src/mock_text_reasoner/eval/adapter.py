"""The text mock's ``EvalAdapter`` for the framework eval harness.

Published as the ``mock-text-reasoner`` entry point in the
``decisionledger.eval_adapters`` group, so ``uv run python -m
eval.runners.harness --reasoner-id mock-text-reasoner`` (``make
eval-mock_text``, one of ``make eval-mocks``) resolves it: the event type
its datasets are written in, the datasets, and an in-process driver into its
pipeline. The mock ships only ``scenarios/`` and ``robustness/`` datasets,
so the RAG policy gate's own eval never selects it, although it binds that
gate to two corpora: its multi-corpus path is shown live, not in the gate's
eval.

Importing this module reads no settings and opens nothing: the harness, the
policy gate's eval, and the corpus loader load every installed adapter or
plugin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from mock_text_reasoner.messages import CustomerMessage
from mock_text_reasoner.registry import MOCK_TEXT_REGISTRATION

if TYPE_CHECKING:
    from app.settings import FrameworkSettings
    from mock_text_reasoner.eval.driver import MockTextEvalDriver

_DATASETS_DIR = Path(__file__).parent / "datasets"


@dataclass(frozen=True)
class MockTextEvalAdapter:
    """Supplies the text mock's event type, datasets, and pipeline driver.

    Attributes:
        reasoner_id: ``mock-text-reasoner``.
        event_type: ``CustomerMessage``.
        datasets_dir: ``scenarios/`` (consistency across orderings of a
            customer's messages) and ``robustness/`` (prompt injection, novel
            texts, schema failure, and fallback; all deterministic).
    """

    reasoner_id: str = MOCK_TEXT_REGISTRATION.reasoner_id
    event_type: type[CustomerMessage] = CustomerMessage
    datasets_dir: Path = field(default=_DATASETS_DIR)

    def build_driver(self, settings: FrameworkSettings) -> MockTextEvalDriver:
        """Open the mock's pipeline against the ledger, in process.

        Args:
            settings: Framework settings (Postgres).

        Returns:
            A connected driver over the mock's plugin and its scripted
            policy double; the caller closes it.
        """
        from mock_text_reasoner.eval.driver import MockTextEvalDriver

        return MockTextEvalDriver(settings)


#: The adapter instance the entry point names.
ADAPTER = MockTextEvalAdapter()
