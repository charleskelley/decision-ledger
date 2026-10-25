"""The text mock's plugin — what a ledger deployment that installs it discovers.

``PLUGIN`` is published under the ``decisionledger.reasoners`` entry point
(name ``mock-text-reasoner``): the registration, and the prompt template
and policy corpora the RAG policy gate reads for this reasoner. The ledger
runs none of the mock's code (DR-31); the mock's CLI submits over HTTP.

``corpus_dir`` is the parent of the two corpora the binding names, one
subdirectory each: ``regulatory/`` (loaded as ``mock-text-regulatory``) and
``practice/`` (``mock-text-practice``). The corpus loader loads one
directory as one corpus id, so each is loaded by its own loader call with
``--corpus-dir`` (``make prepare-mock_text``); the gate reads the parent for
the jurisdictions the mock may filter to, which covers both.

A deployment that does not install the plugin (or leaves it out of
``DECISIONLEDGER_REASONERS``) registers the mock by file
(``registration.yaml``) or over the API instead, and serves the same assets
from the RAG policy gate's asset root: ``assets/`` is laid out exactly as
``<root>/mock-text-reasoner/`` must be, so a copy of it is the mock's
directory there.
"""

from __future__ import annotations

from pathlib import Path

from mock_text_reasoner.registry import MOCK_TEXT_REGISTRATION

#: The mock's gate assets, laid out as its directory under an asset root.
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
#: The parent of the two corpora.
CORPUS_DIR = ASSETS_DIR / "corpus"
#: The regulatory corpus (``mock-text-regulatory``, tier 1).
REGULATORY_DIR = CORPUS_DIR / "regulatory"
#: The practice notes (``mock-text-practice``, tier 2).
PRACTICE_DIR = CORPUS_DIR / "practice"


class MockTextPlugin:
    """``ReasonerPlugin`` implementation for the text mock.

    Attributes:
        registration: ``MOCK_TEXT_REGISTRATION``.
        prompts_dir: ``assets/prompts/`` (``mock-text-v1.yaml``).
        corpus_dir: ``assets/corpus/``, the parent of ``regulatory/`` and
            ``practice/``.
    """

    def __init__(self) -> None:
        """Bind the plugin's static surface."""
        self.registration = MOCK_TEXT_REGISTRATION
        self.prompts_dir = ASSETS_DIR / "prompts"
        self.corpus_dir = CORPUS_DIR


#: The entry-point target:
#: ``mock-text-reasoner = "mock_text_reasoner.plugin:PLUGIN"``.
PLUGIN = MockTextPlugin()
