"""The ATO reasoner's ``EvalAdapter`` for the framework eval harness.

Published as the ``ato-reasoner`` entry point in the
``decisionledger.eval_adapters`` group. ``uv run python -m
eval.runners.harness --reasoner-id ato-reasoner`` resolves it for the
framework's dimensions, and the RAG policy gate's harness (``python -m
policy_rag_gate.eval``) resolves it to read the golden datasets ATO ships for
that gate (``retrieval/``, ``faithfulness/``, ``citations/``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ato_reasoner.events import LoginEvent
from ato_reasoner.registry import ATO_REGISTRATION

if TYPE_CHECKING:
    from app.settings import FrameworkSettings
    from ato_reasoner.eval.driver import PipelineDriver

_DATASETS_DIR = Path(__file__).parent / "datasets"


@dataclass(frozen=True)
class AtoEvalAdapter:
    """Supplies the ATO event type, golden datasets, and pipeline driver.

    Attributes:
        reasoner_id: ``ato-reasoner``; equals the entry point name and the
            registration's ``reasoner_id``.
        event_type: ``LoginEvent``; the consistency and robustness datasets
            are written in it.
        datasets_dir: ``reasoners/account_takeover/src/ato_reasoner/eval/datasets``:
            ``scenarios/`` and ``robustness/`` for the framework harness,
            ``retrieval/``, ``faithfulness/``, and ``citations/`` for the RAG
            policy gate's.
    """

    reasoner_id: str = ATO_REGISTRATION.reasoner_id
    event_type: type[LoginEvent] = LoginEvent
    datasets_dir: Path = field(default=_DATASETS_DIR)

    def build_driver(self, settings: FrameworkSettings) -> PipelineDriver:
        """Open the ATO pipeline against live infrastructure.

        The driver module is imported lazily so loading the adapter (for
        discovery or ``--help``) does not pull in the scorer and embedding
        stacks.

        Args:
            settings: Framework settings (Postgres). The driver reads the
                policy gate's own settings (Elasticsearch, the OpenAI key)
                itself.

        Returns:
            A connected ``PipelineDriver``; the caller closes it.
        """
        from ato_reasoner.eval.driver import (
            PipelineDriver,
        )

        return PipelineDriver(framework_settings=settings)


#: The adapter instance the entry point names.
ADAPTER = AtoEvalAdapter()
