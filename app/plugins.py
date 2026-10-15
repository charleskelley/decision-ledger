"""Reasoner registrations: plugin discovery and the registration file.

Reasoners declare themselves under the ``decisionledger.reasoners`` entry
point group; the entry-point name must equal the plugin's
``registration.reasoner_id``. ``discover_plugins`` loads every declared
plugin (optionally filtered by an allowlist), validates it against the
``ReasonerPlugin`` protocol, and returns the list sorted by name so the
composition order is deterministic. Any malformed plugin raises
``ReasonerPluginError`` so a broken deployment fails at startup rather
than silently running without a reasoner.

A reasoner that runs as its own service needs no plugin installed in the
ledger's deployment: it registers through the deployment's registration file
(``DECISIONLEDGER_REGISTRATIONS``, DR-31), a YAML list of registrations
(``load_registration_file``). ``build_registry`` composes both sources into
the registry the deployment serves — read-only, as the registrations
resource — and refuses a ``reasoner_id`` declared twice.
"""

from __future__ import annotations

import importlib.metadata as metadata
import json
from typing import TYPE_CHECKING

import structlog
import yaml
from pydantic import ValidationError

from app.plugin import ReasonerPlugin
from app.reasoner_registry import StaticReasonerRegistry
from core.exceptions import ReasonerPluginError, RegistrationError
from core.observation import ReasonerRegistration

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from importlib.metadata import EntryPoint
    from pathlib import Path

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
                "ReasonerPlugin protocol (registration, prompts_dir, corpus_dir)",
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


def build_registry(
    plugins: Sequence[ReasonerPlugin],
    *,
    registration_file: Path | None = None,
) -> StaticReasonerRegistry:
    """Compose the plugins' registrations and the registration file's.

    Args:
        plugins: Discovered (or explicitly passed) reasoner plugins.
        registration_file: The deployment's registration file
            (``DECISIONLEDGER_REGISTRATIONS``), or ``None`` for none.

    Returns:
        Registry keyed by ``reasoner_id``, each entry labelled with its
        source (``plugin:<module>`` or ``file:<path>``); intake validation,
        the enforcement profile, and the registrations resource all read
        from it.

    Raises:
        RegistrationError: The file cannot be loaded, or it registers a
            ``reasoner_id`` a plugin (or the file itself) already registers.
        ValueError: Two plugins share a ``reasoner_id``.
    """
    registrations = [p.registration for p in plugins]
    sources = {
        p.registration.reasoner_id: f"plugin:{type(p).__module__}" for p in plugins
    }
    if registration_file is not None:
        label = f"file:{registration_file}"
        for registration in load_registration_file(registration_file):
            reasoner_id = registration.reasoner_id
            if reasoner_id in sources:
                raise RegistrationError(
                    label,
                    f"reasoner_id {reasoner_id!r} is already registered by "
                    f"{sources[reasoner_id]}",
                )
            sources[reasoner_id] = label
            registrations.append(registration)
    return StaticReasonerRegistry(registrations, sources=sources)


def load_registration_file(path: Path) -> list[ReasonerRegistration]:
    """Load the registrations a deployment's registration file declares.

    The file is YAML: a list of registrations, or a mapping whose
    ``registrations`` key holds that list. Each entry has the shape of a
    ``ReasonerRegistration`` serialized as JSON — gate bindings, bypass rules
    with their predicates, ``recordable_actions`` and each jurisdiction
    conflict group as lists, actions by name — and is validated strictly,
    through JSON, exactly as the wire models are.

    Args:
        path: The registration file.

    Returns:
        The registrations, in file order.

    Raises:
        RegistrationError: The file cannot be read or parsed, does not hold
            a list of registrations, an entry fails validation, or two
            entries share a ``reasoner_id``.
    """
    label = f"file:{path}"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as err:
        raise RegistrationError(label, f"cannot be read as YAML: {err}") from err
    if isinstance(data, dict) and set(data) == {"registrations"}:
        data = data["registrations"]
    if not isinstance(data, list):
        raise RegistrationError(
            label, "expected a list of registrations (or {registrations: [...]})"
        )
    registrations: list[ReasonerRegistration] = []
    seen: set[str] = set()
    for index, item in enumerate(data):
        try:
            registration = ReasonerRegistration.model_validate_json(
                json.dumps(item, default=str)
            )
        except ValidationError as err:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in e['loc']) or '<root>'}: {e['msg']}"
                for e in err.errors(include_url=False)
            )
            raise RegistrationError(
                label, f"registration #{index} is invalid: {problems}"
            ) from err
        if registration.reasoner_id in seen:
            raise RegistrationError(
                label, f"reasoner_id {registration.reasoner_id!r} is registered twice"
            )
        seen.add(registration.reasoner_id)
        registrations.append(registration)
    log.info(
        "plugins.registration_file_loaded",
        component="plugins",
        path=str(path),
        reasoner_ids=sorted(seen),
    )
    return registrations


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
