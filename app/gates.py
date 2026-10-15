"""Gate plugin seam and discovery — gates are plugins, like reasoners (DR-28).

A gate joins a DecisionLedger deployment by publishing one object that
satisfies ``GatePlugin`` through the ``decisionledger.gates`` entry-point
group. The framework never imports a gate package: it discovers the plugins
(``discover_gates``), builds each gate once against the shared
``FrameworkServices`` (``build_gates``, called by the bootstrap), and
dispatches to the gates of a decision's gate set by ``gate_id``
(``app/submit.py``), each through ``invoke_gate``.

The seam is five types:

- ``GateKind`` — what a gate is: ``llm``, ``lookup``, or ``rules``.
- ``Gate`` — the runtime object: ``evaluate(GateRequest) -> GateOutput`` and
  ``close()``. A gate owns its own infrastructure (indexes, model clients,
  rule files) and returns the universal ``GateOutput``; anything
  gate-specific goes into the output's ``evidence_snapshot`` / ``extra``.
- ``ConfigAuthorizer`` — an optional ``Gate`` capability: decide, per
  submitting reasoner, whether a validated ``gate_config`` is allowed (for
  example only resources that reasoner ships). The framework calls it at
  intake, in both modes, right after ``config_model`` validation.
- ``ReasonerScope`` — an optional ``Gate`` capability: whether the gate can
  serve a reasoner at all (a gate that loads per-reasoner assets serves only
  the reasoners whose assets it loaded). The framework asks at startup, for
  every registration that binds the gate, so a reasoner the gate would
  refuse on every submission fails the deployment instead.
- ``GatePlugin`` — what the entry point publishes: ``gate_id``, ``kind``,
  ``deterministic``, an optional ``config_model`` that validates
  ``GateContext.gate_config`` at intake, and ``build(framework) -> Gate``.
- ``ComposedGate`` — a built gate paired with the plugin that declared it,
  and whether the deployment mandates it for every reasoner
  (``DECISIONLEDGER_MANDATORY_GATES``, DR-29); ``FrameworkServices.gates``
  maps ``gate_id`` to one.

A malformed plugin, or a mandated gate the deployment does not compose,
raises ``GatePluginError`` at startup, so a broken deployment fails before
it serves a decision. At decision time a gate fails closed instead
(``invoke_gate``): an exception, a timeout past the request's budget, an
output that is not a plain ``GateOutput`` for that gate or that the ledger
could not store as returned, or no gate composed under the id all become a
``failed`` invocation with the reason, which enforcement holds for review
(DR-29), and the decision is still written. Tests inject fake gates by passing
plugins explicitly (``create_app(gate_plugins=[...])``,
``build_framework_services(..., gate_plugins=[...])``) or explicit
``entry_points`` to ``discover_gates``.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata as metadata
import inspect
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

import structlog
from pydantic import BaseModel

from app.monitoring import duration_ms
from core.exceptions import GatePluginError
from core.gate import EvidenceRef, GateOutput, GateVerdict
from core.submission import storage_problem

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from importlib.metadata import EntryPoint
    from typing import Any

    from app.plugin import FrameworkServices
    from core.gate_request import GateRequest
    from core.observation import ReasonerRegistration

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

        The framework enforces ``request.budget_ms`` cooperatively: a gate
        still awaiting when the budget expires is cancelled, and a gate that
        returns after it (for example because it blocked the event loop,
        which no ``await`` can interrupt) is recorded as failed. Either way
        the gate fails closed. So ``evaluate`` must not block the loop: run
        blocking I/O or CPU-bound work in a thread (``asyncio.to_thread``)
        and await it.

        Args:
            request: The framework-built request: decision id, the
                reasoner's submission, this gate's ``GateContext``, and a
                budget in milliseconds.

        Returns:
            A ``GateOutput`` whose ``gate_id`` and ``deterministic`` match
            the plugin's declaration. A gate that cannot reach a verdict
            returns ``verdict=None`` (enforcement holds the decision); one
            that raises fails closed the same way, with the error recorded.
        """
        ...

    def close(self) -> None:
        """Release any connections or models the gate opened."""
        ...


@runtime_checkable
class ConfigAuthorizer(Protocol):
    """Optional ``Gate`` capability: authorize ``gate_config`` per reasoner.

    ``config_model`` validates the shape of a configuration; some gates also
    decide who may send which configuration — a gate that serves several
    reasoners may, for example, accept only resources the submitting
    reasoner ships. A built gate that defines ``authorize_config`` is asked
    at intake, in both modes (recorded decisions included, since their gate
    contexts must stay evaluable for shadow evaluation), after
    ``config_model`` validation; a refusal rejects the submission before any
    gate runs.
    """

    def authorize_config(
        self, config: BaseModel | Mapping[str, Any], *, reasoner_id: str
    ) -> None:
        """Accept ``config`` from ``reasoner_id`` or raise ``ValueError``.

        Args:
            config: The ``config_model`` instance when the plugin declares
                one, else the raw ``gate_config`` mapping.
            reasoner_id: The submitting reasoner.

        Raises:
            ValueError: ``reasoner_id`` may not submit ``config``; the
                message says why.
        """
        ...


@runtime_checkable
class ReasonerScope(Protocol):
    """Optional ``Gate`` capability: which reasoners the gate can serve.

    A gate that serves reasoners from assets it loads when it is built (a
    prompt template, a corpus) cannot serve a reasoner registered without
    them — for example one registered through the deployment's registration
    file, with no plugin installed — and would refuse every submission it is
    addressed in. ``build_gates`` asks ``serves`` for every registration that
    binds the gate and refuses the deployment when the answer is no.
    """

    def serves(self, reasoner_id: str) -> bool:
        """Whether submissions of ``reasoner_id`` can be served at all.

        Args:
            reasoner_id: A reasoner whose registration binds this gate.

        Returns:
            ``False`` when every submission of that reasoner addressed to
            this gate would be refused.
        """
        ...


@runtime_checkable
class GatePlugin(Protocol):
    """The object a gate publishes through the ``decisionledger.gates`` group.

    Attributes:
        gate_id: Stable identifier gate contexts and gate bindings name.
            Must equal the entry-point name.
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
        mandatory: ``True`` when the deployment runs this gate on every
            decision of every reasoner (``DECISIONLEDGER_MANDATORY_GATES``),
            whether or not a registration binds it.
    """

    plugin: GatePlugin
    gate: Gate
    mandatory: bool = False

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
    *,
    mandatory: Iterable[str] = (),
) -> dict[str, ComposedGate]:
    """Build every gate against the shared framework services.

    Args:
        plugins: Gate plugins to compose (discovered or passed explicitly).
        framework: Shared services handed to each ``plugin.build``.
        mandatory: Gate ids the deployment runs on every decision
            (``DECISIONLEDGER_MANDATORY_GATES``); each must be among
            ``plugins``.

    Once every gate is built, the gate set of every registration the
    deployment serves (``framework.registry``: installed plugins and the
    registration file) is checked against them, so a deployment that cannot
    serve a reasoner fails here rather than on every decision:

    - a gate a registration binds as mandatory must be composed (an
      advisory binding to a missing gate is only logged: that reasoner's
      gated decisions fail closed at that gate);
    - a composed gate a registration binds must serve that reasoner, when
      the gate says whom it serves (``ReasonerScope``): otherwise intake
      would refuse every submission of the reasoner;
    - a gate the deployment mandates must accept, from every composed
      reasoner that does not bind it, the empty configuration it will run
      with: its ``config_model`` validates ``{}`` and, when the built gate
      is a ``ConfigAuthorizer``, it authorizes that for the reasoner.

    Returns:
        Composed gates keyed by ``gate_id``, in plugin order, the mandated
        ones flagged ``mandatory``.

    Raises:
        GatePluginError: A mandated gate is not among ``plugins`` (raised
            before anything is built), a plugin is malformed, two plugins
            share a ``gate_id``, ``build`` returns an object that does not
            satisfy ``Gate`` (including a synchronous ``evaluate``, which
            would only fail once a decision reaches it), or a composed
            reasoner's gate set cannot be served (above). The rejected
            object and every gate built are closed first; any exception
            ``build`` raises propagates the same way.
    """
    mandated = frozenset(mandatory)
    missing = sorted(mandated - {plugin.gate_id for plugin in plugins})
    if missing:
        raise GatePluginError(
            ",".join(missing),
            "DECISIONLEDGER_MANDATORY_GATES names gate(s) this deployment does "
            f"not compose (composed: {sorted(p.gate_id for p in plugins)})",
        )
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
            composed[plugin.gate_id] = ComposedGate(
                plugin=plugin, gate=gate, mandatory=plugin.gate_id in mandated
            )
        _check_gate_sets(_served_registrations(framework), composed)
    except Exception:
        _close_quietly(composed)
        raise
    return composed


def _served_registrations(framework: object) -> list[ReasonerRegistration]:
    """Every registration the services serve: the registry's, else the plugins'."""
    registry = getattr(framework, "registry", None)
    if registry is not None and hasattr(registry, "entries"):
        served: Iterable[Any] = registry.entries()
        return [entry.registration for entry in served]
    plugins: Iterable[Any] = getattr(framework, "reasoners", ())
    return [plugin.registration for plugin in plugins]


def _check_gate_sets(
    registrations: Sequence[ReasonerRegistration],
    gates: Mapping[str, ComposedGate],
) -> None:
    """Fail on a reasoner gate set this deployment's gates cannot serve."""
    for registration in registrations:
        reasoner_id = registration.reasoner_id
        for binding in registration.gates:
            composed = gates.get(binding.gate_id)
            if composed is not None:
                gate = composed.gate
                if isinstance(gate, ReasonerScope) and not gate.serves(reasoner_id):
                    raise GatePluginError(
                        binding.gate_id,
                        f"reasoner {reasoner_id!r} binds this gate, which does not "
                        "serve it (a gate that loads per-reasoner assets serves "
                        "only the reasoners whose assets it loaded): intake would "
                        "refuse every submission of that reasoner. Install the "
                        "reasoner's plugin, or remove the binding.",
                    )
                continue
            if binding.mandatory:
                raise GatePluginError(
                    binding.gate_id,
                    f"reasoner {reasoner_id!r} binds this gate as mandatory, but "
                    f"the deployment does not compose it (composed: {sorted(gates)})",
                )
            log.warning(
                "gates.advisory_binding_not_composed",
                component="gates",
                gate_id=binding.gate_id,
                reasoner_id=reasoner_id,
                msg="gated decisions of this reasoner fail closed at this gate",
            )
        for gate_id, composed in gates.items():
            if composed.mandatory and gate_id not in registration.gate_ids:
                _check_unbound_mandate(composed, reasoner_id)


def _check_unbound_mandate(composed: ComposedGate, reasoner_id: str) -> None:
    """A mandated gate the reasoner does not bind must take an empty config."""
    config: BaseModel | Mapping[str, Any] = {}
    config_model = composed.plugin.config_model
    try:
        if config_model is not None:
            config = config_model.model_validate({})
        if isinstance(composed.gate, ConfigAuthorizer):
            composed.gate.authorize_config(config, reasoner_id=reasoner_id)
    except ValueError as err:  # pydantic.ValidationError is a ValueError
        raise GatePluginError(
            composed.gate_id,
            f"DECISIONLEDGER_MANDATORY_GATES mandates this gate for every "
            f"reasoner, but reasoner {reasoner_id!r} does not bind it and the "
            f"gate refuses the empty configuration it would run with ({err}); "
            "bind it in that reasoner's registration or stop mandating it",
        ) from err


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


#: Why a gate invocation failed closed, as recorded in the gate's record.
FailureKind = Literal["not_composed", "timeout", "error", "malformed_output"]


@dataclass(frozen=True)
class GateInvocation:
    """The outcome of invoking one gate for one decision (or shadow run).

    Args:
        gate_id: The gate.
        output: The universal output, when the gate returned a usable one.
        invoked: ``True`` when the framework called ``evaluate`` (every case
            but a gate that is not composed).
        failure: Why the invocation failed closed, or ``None`` when it ran.
        reason: Human-readable failure reason, or ``None`` when it ran.
        duration_ms: Wall-clock time of the call, or ``None`` when the gate
            was not invoked.
    """

    gate_id: str
    output: GateOutput | None
    invoked: bool
    failure: FailureKind | None = None
    reason: str | None = None
    duration_ms: float | None = None

    @property
    def ran(self) -> bool:
        """``True`` when the gate returned a usable output."""
        return self.failure is None


async def invoke_gate(
    composed: ComposedGate | None,
    request: GateRequest,
) -> GateInvocation:
    """Invoke one gate for one request, failing closed on anything unusable.

    Never raises for a gate's own failure (DR-29): the result says why the
    gate produced no usable output, and enforcement routes that gate to its
    schema-failure tier. Only the caller's own cancellation propagates; a
    ``CancelledError`` the gate raises while its caller is not being
    cancelled (a helper task someone else cancelled, say) is the gate's
    failure.

    The budget is cooperative. The gate runs inline in the caller's task
    under ``asyncio.timeout``: a gate still awaiting at the deadline is
    cancelled, and one that answers only after its budget (because it
    blocked the event loop, which no deadline can interrupt, or ignored its
    cancellation) fails when it returns. Because the gate runs inline, the
    clock starts when this gate's task first runs and stops when the gate
    returns, so a sibling that blocked the loop before this gate started or
    after it answered is not charged to it.

    Args:
        composed: The composed gate addressed by ``request.gate_context``,
            or ``None`` when the deployment composes no gate under that id.
        request: The framework-built request. Its ``budget_ms`` bounds the
            call.

    Returns:
        A ``GateInvocation``: the output when the gate ran; otherwise the
        failure kind and reason — not composed; a timeout (the deadline
        passed while the gate was awaiting, or the gate answered after its
        budget); an exception the gate raised, a ``TimeoutError`` of its own
        included; or an output that is not exactly the universal
        ``GateOutput`` (with ``GateVerdict`` and ``EvidenceRef`` inside) for
        this gate with its declared ``deterministic`` flag, or that the
        ledger could not store and reload as returned. A non-universal or
        unstorable output is refused because the bundle the caller receives
        must equal the bundle the store reloads. A reason that quotes the
        gate's own text has NUL and unencodable characters escaped, so the
        record stays storable.
    """
    gate_id = request.gate_context.gate_id
    log_ctx = {
        "component": "gates",
        "gate_id": gate_id,
        "decision_id": request.decision_id,
        "event_id": request.submission.event_id,
    }
    if composed is None:
        reason = f"no gate {gate_id!r} is composed in this deployment"
        return _failed(gate_id, "not_composed", reason, None, log_ctx, invoked=False)

    budget_ms = request.budget_ms
    deadline = asyncio.timeout(budget_ms / 1_000)
    t0 = time.perf_counter()
    try:
        async with deadline:
            output = await composed.gate.evaluate(request)
    except TimeoutError as err:
        if deadline.expired():
            reason = f"timed out after {budget_ms} ms (budget)"
            return _failed(gate_id, "timeout", reason, duration_ms(t0), log_ctx)
        reason = f"raised {type(err).__name__}: {err}"  # the gate's own timeout
        return _failed(gate_id, "error", reason, duration_ms(t0), log_ctx)
    except asyncio.CancelledError as err:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise  # the caller is being cancelled
        reason = f"raised CancelledError: {err}"
        return _failed(gate_id, "error", reason, duration_ms(t0), log_ctx)
    except Exception as err:
        reason = f"raised {type(err).__name__}: {err}"
        return _failed(gate_id, "error", reason, duration_ms(t0), log_ctx)
    elapsed = duration_ms(t0)
    if deadline.expired():
        reason = (
            f"timed out after {budget_ms} ms (budget): it ignored its "
            f"cancellation and returned after {elapsed} ms"
        )
        return _failed(gate_id, "timeout", reason, elapsed, log_ctx)
    if elapsed > budget_ms:
        reason = (
            f"exceeded its budget: returned after {elapsed} ms (budget {budget_ms} ms)"
        )
        return _failed(gate_id, "timeout", reason, elapsed, log_ctx)
    problem = _output_problem(composed, output)
    if problem is not None:
        reason = f"returned a malformed output: {problem}"
        return _failed(gate_id, "malformed_output", reason, elapsed, log_ctx)
    return GateInvocation(
        gate_id=gate_id, output=output, invoked=True, duration_ms=elapsed
    )


def _failed(
    gate_id: str,
    failure: FailureKind,
    reason: str,
    elapsed: float | None,
    log_ctx: Mapping[str, str],
    *,
    invoked: bool = True,
) -> GateInvocation:
    """Log and return a failed invocation, with its reason made storable."""
    reason = _storable_text(reason)
    log.error(f"gates.{failure}", reason=reason, duration_ms=elapsed, **log_ctx)
    return GateInvocation(
        gate_id=gate_id,
        output=None,
        invoked=invoked,
        failure=failure,
        reason=reason,
        duration_ms=elapsed,
    )


def _storable_text(text: str) -> str:
    """Escape what the ledger cannot store: NUL and unencodable characters."""
    escaped = text.encode("utf-8", "backslashreplace").decode("utf-8")
    return escaped.replace("\x00", "\\x00")


def _output_problem(composed: ComposedGate, output: object) -> str | None:
    """Describe why ``output`` is not a storable, replayable output, or ``None``."""
    gate_id = composed.gate_id
    if type(output) is not GateOutput:
        return (
            f"evaluate() returned {type(output).__name__}; gates return the "
            "universal GateOutput and keep gate-specific fields in "
            "evidence_snapshot / extra (DR-28)"
        )
    if output.verdict is not None and type(output.verdict) is not GateVerdict:
        return (
            f"the verdict is a {type(output.verdict).__name__}; the verdict must "
            "be the universal GateVerdict (record explanations in extra)"
        )
    foreign = {
        type(ref).__name__ for ref in output.evidence if type(ref) is not EvidenceRef
    }
    if foreign:
        return (
            f"evidence of type(s) {sorted(foreign)}; evidence references must "
            "be the universal EvidenceRef"
        )
    if output.gate_id != gate_id:
        return f"a GateOutput for gate {output.gate_id!r}"
    if output.deterministic != composed.plugin.deterministic:
        return (
            f"deterministic={output.deterministic}, but the plugin declares "
            f"deterministic={composed.plugin.deterministic}"
        )
    return _storage_problem(output)


def _storage_problem(output: GateOutput) -> str | None:
    """Describe why the ledger could not store ``output`` as returned, or ``None``.

    The bundle stores the output as JSON in Postgres, and replay reloads it:
    every value must be one JSON and the database can hold (no NUL, no
    non-finite number, valid Unicode) and the output must reload equal to
    itself (a ``model_construct``-built output can hold anything).
    """
    try:
        problem = storage_problem(output.model_dump(mode="json", warnings="error"))
        if problem is None:
            text = output.model_dump_json(warnings="error")
            if GateOutput.model_validate_json(text) != output:
                problem = "a value that does not survive the JSON round trip"
    except ValueError as err:  # PydanticSerializationError or ValidationError
        problem = f"a value that cannot be serialized as JSON ({type(err).__name__})"
    if problem is None:
        return None
    return f"it holds {problem}, which the ledger cannot store as returned"


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
