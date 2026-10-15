"""Reasoner plugin discovery via Python entry points.

Reasoners declare themselves under the ``decisionledger.reasoners`` entry
point group; the entry-point name must equal the plugin's
``registration.reasoner_id``. ``discover_plugins`` loads every declared
plugin (optionally filtered by an allowlist), validates it against the
``ReasonerPlugin`` protocol, and returns the list sorted by name so the
composition order is deterministic. Any malformed plugin raises
``ReasonerPluginError`` so a broken deployment fails at startup rather
than silently running without a reasoner.
"""

from __future__ import annotations

import importlib.metadata as metadata
from typing import TYPE_CHECKING

import structlog

from app.plugin import ReasonerPlugin
from app.reasoner_registry import StaticReasonerRegistry
from core.exceptions import ReasonerPluginError

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from importlib.metadata import EntryPoint

    from app.settings import FrameworkSettings

log = structlog.get_logger(__name__)

#: Entry point group every runtime reasoner plugin registers under.
PLUGIN_GROUP = "decisionledger.reasoners"


def discover_plugins(
    *,
    entry_points: Iterable[EntryPoint] | None = None,
    allow: frozenset[str] | None = None,
) -> list[ReasonerPlugin]:
    """Load and validate every reasoner plugin declared for this process.

    Args:
        entry_points: Explicit entry points to load instead of querying
            ``importlib.metadata`` for ``PLUGIN_GROUP``. Tests use this to
            inject in-module fakes.
        allow: Optional allowlist of ``reasoner_id`` values. Entry points
            whose name is not in the set are skipped without being loaded.
            ``None`` means every declared plugin is loaded.

    Returns:
        Plugins sorted by entry-point name.

    Raises:
        ReasonerPluginError: An entry point fails to load, the loaded object
            does not satisfy ``ReasonerPlugin``, its name differs from its
            ``registration.reasoner_id``, or two plugins share an id.
    """
    eps = (
        list(entry_points)
        if entry_points is not None
        else list(_installed_entry_points())
    )
    eps.sort(key=lambda ep: ep.name)

    plugins: list[ReasonerPlugin] = []
    seen: dict[str, str] = {}
    for ep in eps:
        if allow is not None and ep.name not in allow:
            log.info(
                "plugins.skipped",
                component="plugins",
                plugin_name=ep.name,
                reason="not in DECISIONLEDGER_REASONERS allowlist",
            )
            continue

        try:
            obj = ep.load()
        except Exception as err:
            raise ReasonerPluginError(
                ep.name, f"entry point {ep.value!r} failed to load: {err}"
            ) from err

        if not isinstance(obj, ReasonerPlugin):
            raise ReasonerPluginError(
                ep.name,
                f"object {obj!r} loaded from {ep.value!r} does not satisfy the "
                "ReasonerPlugin protocol (registration, router, prompts_dir, "
                "corpus_dir, build_services)",
            )

        reasoner_id = obj.registration.reasoner_id
        if reasoner_id != ep.name:
            raise ReasonerPluginError(
                ep.name,
                f"entry point name must equal registration.reasoner_id "
                f"({reasoner_id!r})",
            )
        if reasoner_id in seen:
            raise ReasonerPluginError(
                ep.name,
                f"duplicate reasoner_id {reasoner_id!r}; already provided by "
                f"{seen[reasoner_id]!r}",
            )
        seen[reasoner_id] = ep.value
        plugins.append(obj)
        log.info(
            "plugins.loaded",
            component="plugins",
            reasoner_id=reasoner_id,
            entry_point=ep.value,
        )

    return plugins


def build_registry(plugins: Sequence[ReasonerPlugin]) -> StaticReasonerRegistry:
    """Collect every plugin's registration into a ``StaticReasonerRegistry``.

    Args:
        plugins: Discovered plugins.

    Returns:
        Registry keyed by ``reasoner_id``; intake validation and the
        enforcement profile both read from it.
    """
    return StaticReasonerRegistry([p.registration for p in plugins])


def allowlist_from_settings(fw: FrameworkSettings) -> frozenset[str] | None:
    """Parse ``DECISIONLEDGER_REASONERS`` into an allowlist.

    Args:
        fw: Framework settings carrying the comma-separated allowlist.

    Returns:
        The set of allowed ``reasoner_id`` values, or ``None`` when the
        setting is blank (every installed plugin is allowed).
    """
    ids = frozenset(
        part.strip() for part in fw.decisionledger_reasoners.split(",") if part.strip()
    )
    return ids or None


def _installed_entry_points() -> Iterable[EntryPoint]:
    """Return the entry points installed under ``PLUGIN_GROUP``."""
    return metadata.entry_points(group=PLUGIN_GROUP)
