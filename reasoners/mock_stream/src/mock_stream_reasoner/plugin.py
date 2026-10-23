"""The stream mock's plugin — what a ledger deployment that installs it discovers.

``PLUGIN`` is published under the ``decisionledger.reasoners`` entry point
(name ``mock-stream-reasoner``): the registration, and the prompt template
and policy corpus the RAG policy gate reads for this reasoner. The ledger
runs none of the mock's code (DR-31); the mock runs as its own service and
submits over HTTP.

A deployment that does not install the plugin (or leaves it out of
``DECISIONLEDGER_REASONERS``) registers the mock by file
(``registration.yaml``) or over the API instead, and serves the same assets
from the RAG policy gate's asset root: ``assets/`` is laid out exactly as
``<root>/mock-stream-reasoner/`` must be, so a copy of it is the mock's
directory there.
"""

from __future__ import annotations

from pathlib import Path

from mock_stream_reasoner.registry import MOCK_STREAM_REGISTRATION

#: The mock's gate assets, laid out as its directory under an asset root.
ASSETS_DIR = Path(__file__).resolve().parent / "assets"


class MockStreamPlugin:
    """``ReasonerPlugin`` implementation for the stream mock.

    Attributes:
        registration: ``MOCK_STREAM_REGISTRATION``.
        prompts_dir: ``assets/prompts/`` (``mock-stream-v1.yaml``).
        corpus_dir: ``assets/corpus/`` (the practice notes loaded as
            ``mock-stream-practices``).
    """

    def __init__(self) -> None:
        """Bind the plugin's static surface."""
        self.registration = MOCK_STREAM_REGISTRATION
        self.prompts_dir = ASSETS_DIR / "prompts"
        self.corpus_dir = ASSETS_DIR / "corpus"


#: The entry-point target:
#: ``mock-stream-reasoner = "mock_stream_reasoner.plugin:PLUGIN"``.
PLUGIN = MockStreamPlugin()
