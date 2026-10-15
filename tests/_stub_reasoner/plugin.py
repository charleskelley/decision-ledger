"""Stub reasoner plugin — the object the framework discovers and composes.

``PLUGIN`` satisfies ``app.plugin.ReasonerPlugin``. Tests publish it as an
``importlib.metadata.EntryPoint`` named ``stub-reasoner`` (the value is
``tests._stub_reasoner.plugin:PLUGIN``) or pass it to
``create_app(plugins=[PLUGIN])`` directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from tests._stub_reasoner.api import router
from tests._stub_reasoner.registry import STUB_REGISTRATION

if TYPE_CHECKING:
    from app.plugin import FrameworkServices

_PACKAGE_DIR = Path(__file__).resolve().parent


class StubServices:
    """Request-time services for the stub reasoner: nothing but a close flag.

    Attributes:
        framework: The ``FrameworkServices`` the plugin was built against.
        closed: ``True`` once the framework has called ``close()``.
    """

    def __init__(self, framework: FrameworkServices) -> None:
        """Remember the framework services; open no connections."""
        self.framework = framework
        self.closed = False

    def close(self) -> None:
        """Mark the services closed (there is nothing to release)."""
        self.closed = True


class StubPlugin:
    """``ReasonerPlugin`` implementation for the stub reasoner.

    Attributes:
        registration: ``STUB_REGISTRATION``.
        router: The ``/api/v1/stub`` router.
        prompts_dir: ``tests/_stub_reasoner/prompts`` (holds ``stub-v1.yaml``).
        corpus_dir: ``tests/_stub_reasoner/corpus`` (two Markdown policies).
        services: The most recently built ``StubServices`` (``None`` until
            ``build_services`` runs); lets tests assert on lifecycle.
    """

    def __init__(self) -> None:
        """Bind the plugin's static surface."""
        self.registration = STUB_REGISTRATION
        self.router = router
        self.prompts_dir = _PACKAGE_DIR / "prompts"
        self.corpus_dir = _PACKAGE_DIR / "corpus"
        self.services: StubServices | None = None

    def build_services(self, framework: FrameworkServices) -> StubServices:
        """Build the stub's (empty) services against the shared framework.

        Args:
            framework: Shared services built by the framework bootstrap.

        Returns:
            A ``StubServices`` whose ``close()`` is a no-op flag flip.
        """
        self.services = StubServices(framework)
        return self.services


#: The entry-point target: ``stub-reasoner = "tests._stub_reasoner.plugin:PLUGIN"``.
PLUGIN = StubPlugin()
