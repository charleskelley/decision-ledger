"""Gate plugin seam and discovery — gates are plugins, like reasoners (DR-28).

A gate joins a DecisionLedger deployment by publishing one object that
satisfies ``GatePlugin`` through the ``decisionledger.gates`` entry-point
group. The framework never imports a gate package: it discovers the plugins
(``discover_gates``), builds each gate once against the shared
``FrameworkServices`` (``build_gates``, called by the bootstrap), and
dispatches to a gate by the ``gate_id`` an observation's ``GateContext``
names (``app/decide.py``).

The seam is four types:

- ``GateKind`` — what a gate is: ``llm``, ``lookup``, or ``rules``.
- ``Gate`` — the runtime object: ``evaluate(GateRequest) -> GateOutput`` and
  ``close()``. A gate owns its own infrastructure (indexes, model clients,
  rule files) and returns the universal ``GateOutput``; anything
  gate-specific goes into the output's ``evidence_snapshot`` / ``extra``.
- ``GatePlugin`` — what the entry point publishes: ``gate_id``, ``kind``,
  ``deterministic``, an optional ``config_model`` that validates
  ``GateContext.gate_config`` at intake, and ``build(framework) -> Gate``.
- ``ComposedGate`` — a built gate paired with the plugin that declared it;
  ``FrameworkServices.gates`` maps ``gate_id`` to one.

A malformed plugin raises ``GatePluginError`` at startup, so a broken
deployment fails before it serves a decision. Tests inject fake gates by
passing plugins explicitly (``create_app(gate_plugins=[...])``,
``build_framework_services(..., gate_plugins=[...])``) or explicit
``entry_points`` to ``discover_gates``.
"""

from __future__ import annotations

import contextlib
import importlib.metadata as metadata
import inspect
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog
from pydantic import BaseModel

from core.exceptions import GatePluginError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from importlib.metadata import EntryPoint

    from app.plugin import FrameworkServices
    from core.gate import GateOutput
    from core.gate_request import GateRequest

log = structlog.get_logger(__name__)

#: Entry point group every gate plugin registers under.
GATE_GROUP = "decisionledger.gates"


class GateKind(StrEnum):
    """What kind of decision procedure a gate runs.

    Attributes:
        LLM: A model-backed gate (non-deterministic by nature).
        LOOKUP: An adapter to an enterprise system of record (a
            do-not-contact list, a sanctions screen).
        RULES: Declarative rules over the reasoner's evidence.
    """

    LLM = "llm"
    LOOKUP = "lookup"
    RULES = "rules"


@runtime_checkable
class Gate(Protocol):
    """A built gate: evaluates one request, owns and releases its resources."""

    async def evaluate(self, request: GateRequest) -> GateOutput:
        """Evaluate one decision and return the universal ``GateOutput``.

        Args:
            request: The framework-built request: decision id, the intake
                observation, this gate's ``GateContext``, and a budget.

        Returns:
            A ``GateOutput`` whose ``gate_id`` and ``deterministic`` match
            the plugin's declaration. A gate that cannot reach a verdict
            returns ``verdict=None`` (enforcement holds the decision).
        """
        ...

    def close(self) -> None:
        """Release any connections or models the gate opened."""
        ...


@runtime_checkable
class GatePlugin(Protocol):
    """The object a gate publishes through the ``decisionledger.gates`` group.

    Attributes:
        gate_id: Stable identifier observations name in
            ``GateContext.gate_id``. Must equal the entry-point name.
        kind: The gate's ``GateKind``.
        deterministic: ``True`` when the gate is a pure function of its
            request. Enforcement skips Tiers 2 and 4 for deterministic gates.
        config_model: Pydantic model that validates ``gate_config`` at
            intake, or ``None`` when the gate validates on consumption.
    """

    gate_id: str
    kind: GateKind
    deterministic: bool
    config_model: type[BaseModel] | None

    def build(self, framework: FrameworkServices) -> Gate:
        """Build the runtime gate against the shared framework services.

        Args:
            framework: Shared services (settings, Postgres, the bundle store,
                the reasoner registry, the composed reasoner plugins). The
                gate map is not yet populated: gates never reach each other.

        Returns:
            The runtime gate; the framework closes it on shutdown.
        """
        ...


@dataclass(frozen=True)
class ComposedGate:
    """A built gate together with the plugin that declared it.

    Args:
        plugin: The discovered plugin (metadata and ``config_model``).
        gate: The runtime gate ``plugin.build`` returned.
    """

    plugin: GatePlugin
    gate: Gate

    @property
    def gate_id(self) -> str:
        """The gate's identifier (from its plugin)."""
        return self.plugin.gate_id


def discover_gates(
    *,
    entry_points: Iterable[EntryPoint] | None = None,
) -> list[GatePlugin]:
    """Load and validate every gate plugin declared for this process.

    Args:
        entry_points: Explicit entry points to load instead of querying
            ``importlib.metadata`` for ``GATE_GROUP``. Tests use this to
            inject in-module fakes.

    Returns:
        Plugins sorted by entry-point name.

    Raises:
        GatePluginError: An entry point fails to load, the loaded object does
            not satisfy ``GatePlugin`` (or declares an unknown ``kind``, a
            non-bool ``deterministic``, or a ``config_model`` that is not a
            Pydantic model), its name differs from its ``gate_id``, or two
            plugins share an id.
    """
    eps = (
        list(entry_points)
        if entry_points is not None
        else list(metadata.entry_points(group=GATE_GROUP))
    )
    eps.sort(key=lambda ep: ep.name)

    plugins: list[GatePlugin] = []
    seen: dict[str, str] = {}
    for ep in eps:
        try:
            obj = ep.load()
        except Exception as err:
            raise GatePluginError(
                ep.name, f"entry point {ep.value!r} failed to load: {err}"
            ) from err

        plugin = check_gate_plugin(obj, name=ep.name)
        if plugin.gate_id in seen:
            raise GatePluginError(
                ep.name,
                f"duplicate gate_id {plugin.gate_id!r}; already provided by "
                f"{seen[plugin.gate_id]!r}",
            )
        seen[plugin.gate_id] = ep.value
        plugins.append(plugin)
        log.info(
            "gates.loaded",
            component="gates",
            gate_id=plugin.gate_id,
            kind=str(plugin.kind),
            deterministic=plugin.deterministic,
            entry_point=ep.value,
        )
    return plugins


def check_gate_plugin(obj: object, *, name: str) -> GatePlugin:
    """Validate that ``obj`` is a well-formed ``GatePlugin`` named ``name``.

    Args:
        obj: The candidate plugin object.
        name: The entry-point name (or, for an explicitly passed plugin, the
            id it is registered under).

    Returns:
        ``obj``, typed as a ``GatePlugin``.

    Raises:
        GatePluginError: The object is not a well-formed gate plugin.
    """
    if not isinstance(obj, GatePlugin):
        raise GatePluginError(
            name,
            f"object {obj!r} does not satisfy the GatePlugin protocol "
            "(gate_id, kind, deterministic, config_model, build)",
        )
    if obj.gate_id != name:
        raise GatePluginError(
            name, f"entry point name must equal gate_id ({obj.gate_id!r})"
        )
    try:
        GateKind(obj.kind)
    except ValueError:
        raise GatePluginError(
            name,
            f"unknown kind {obj.kind!r}; expected one of "
            f"{sorted(k.value for k in GateKind)}",
        ) from None
    if not isinstance(obj.deterministic, bool):
        raise GatePluginError(name, "deterministic must be a bool")
    config_model = obj.config_model
    if config_model is not None and not (
        isinstance(config_model, type) and issubclass(config_model, BaseModel)
    ):
        raise GatePluginError(
            name, "config_model must be a Pydantic BaseModel subclass or None"
        )
    return obj


def build_gates(
    plugins: Sequence[GatePlugin],
    framework: FrameworkServices,
) -> dict[str, ComposedGate]:
    """Build every gate against the shared framework services.

    Args:
        plugins: Gate plugins to compose (discovered or passed explicitly).
        framework: Shared services handed to each ``plugin.build``.

    Returns:
        Composed gates keyed by ``gate_id``, in plugin order.

    Raises:
        GatePluginError: A plugin is malformed, two plugins share a
            ``gate_id``, or ``build`` returns an object that does not satisfy
            ``Gate`` (including a synchronous ``evaluate``, which would only
            fail once a decision is routed to it). The rejected object and the
            gates built before the failure are closed first; any exception
            ``build`` raises propagates the same way.
    """
    composed: dict[str, ComposedGate] = {}
    try:
        for plugin in plugins:
            check_gate_plugin(plugin, name=plugin.gate_id)
            if plugin.gate_id in composed:
                raise GatePluginError(
                    plugin.gate_id, "two composed plugins share this gate_id"
                )
            gate = plugin.build(framework)
            problem = _gate_problem(gate)
            if problem is not None:
                with contextlib.suppress(Exception):
                    gate.close()
                raise GatePluginError(
                    plugin.gate_id, f"build() returned {gate!r}, which {problem}"
                )
            composed[plugin.gate_id] = ComposedGate(plugin=plugin, gate=gate)
    except Exception:
        _close_quietly(composed)
        raise
    return composed


def close_gates(gates: Mapping[str, ComposedGate]) -> None:
    """Close every composed gate, in reverse composition order.

    Every gate gets its ``close()`` call even when an earlier one raises; the
    first error is re-raised once all of them have run.

    Args:
        gates: The composed gates to close.
    """
    first_error: Exception | None = None
    for composed in reversed(list(gates.values())):
        try:
            composed.gate.close()
        except Exception as err:
            log.error(
                "gates.close_failed",
                component="gates",
                gate_id=composed.gate_id,
                error=str(err),
            )
            first_error = first_error or err
    if first_error is not None:
        raise first_error


def _gate_problem(gate: object) -> str | None:
    """Describe why ``gate`` is not a usable ``Gate``, or return ``None``."""
    if not isinstance(gate, Gate):
        return "does not satisfy the Gate protocol (evaluate, close)"
    if not inspect.iscoroutinefunction(gate.evaluate):
        return "has a synchronous evaluate(); the framework awaits it"
    return None


def _close_quietly(gates: Mapping[str, ComposedGate]) -> None:
    """Close gates during failure cleanup without masking the original error."""
    for composed in reversed(list(gates.values())):
        with contextlib.suppress(Exception):
            composed.gate.close()
