"""The example reasoner's plugin — what a ledger deployment discovers.

``PLUGIN`` is published under the ``decisionledger.reasoners`` entry point
(name ``example-reasoner``): the registration, and the prompt template and
policy corpus the RAG policy gate reads for this reasoner. The ledger runs
none of the example's code (DR-31); the example submits through a client.
"""

from __future__ import annotations

from pathlib import Path

from example_reasoner.registry import EXAMPLE_REGISTRATION

_PACKAGE_DIR = Path(__file__).resolve().parent


class ExamplePlugin:
    """``ReasonerPlugin`` implementation for the example reasoner.

    Attributes:
        registration: ``EXAMPLE_REGISTRATION``.
        prompts_dir: ``prompts/`` (``example-v1.yaml``, ``example-v2.yaml``).
        corpus_dir: ``corpus/`` (three Markdown policy documents).
    """

    def __init__(self) -> None:
        """Bind the plugin's static surface."""
        self.registration = EXAMPLE_REGISTRATION
        self.prompts_dir = _PACKAGE_DIR / "prompts"
        self.corpus_dir = _PACKAGE_DIR / "corpus"


#: The entry-point target: ``example-reasoner = "example_reasoner.plugin:PLUGIN"``.
PLUGIN = ExamplePlugin()
