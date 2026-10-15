"""Stub reasoner plugin — the object the framework discovers and composes.

``PLUGIN`` satisfies ``app.plugin.ReasonerPlugin``: the stub's registration
and the prompt and corpus directories a gate may read. Tests publish it as an
``importlib.metadata.EntryPoint`` named ``stub-reasoner`` (the value is
``tests._stub_reasoner.plugin:PLUGIN``) or pass it to
``create_app(plugins=[PLUGIN])`` directly. The ledger runs no reasoner code
(DR-31), so the plugin publishes nothing to run.
"""

from __future__ import annotations

from pathlib import Path

from tests._stub_reasoner.registry import STUB_REGISTRATION

_PACKAGE_DIR = Path(__file__).resolve().parent


class StubPlugin:
    """``ReasonerPlugin`` implementation for the stub reasoner.

    Attributes:
        registration: ``STUB_REGISTRATION``.
        prompts_dir: ``tests/_stub_reasoner/prompts`` (holds ``stub-v1.yaml``).
        corpus_dir: ``tests/_stub_reasoner/corpus`` (two Markdown policies).
    """

    def __init__(self) -> None:
        """Bind the plugin's static surface."""
        self.registration = STUB_REGISTRATION
        self.prompts_dir = _PACKAGE_DIR / "prompts"
        self.corpus_dir = _PACKAGE_DIR / "corpus"


#: The entry-point target: ``stub-reasoner = "tests._stub_reasoner.plugin:PLUGIN"``.
PLUGIN = StubPlugin()
