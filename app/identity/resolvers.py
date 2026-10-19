"""Identity resolver plugins, discovery, and the ledger's identity step (DR-32).

An enterprise supplies its identity resolver as a plugin under the
``decisionledger.identity_resolvers`` entry-point group, the way reasoners
and gates join a deployment. One resolver is active per deployment, named by
``DECISIONLEDGER_IDENTITY_RESOLVER``; blank (or ``passthrough``) keeps the
passthrough default even when resolver plugins are installed, so a
deployment changes how it identifies subjects only by saying so.

The seam is three types:

- ``IdentityResolverPlugin`` — what the entry point publishes: a
  ``resolver_id`` (the entry-point name, and the ``resolved_by`` of every
  identity it returns), the ``relationship_types`` it emits (declared here,
  validated on every identity it returns), and ``build(framework)``, which
  returns the runtime ``core.identity.IdentityResolver``.
- ``ComposedResolver`` — a built resolver with its plugin.
- ``IdentityService`` — the ledger's identity step. ``identify`` runs on
  every decision, after intake validation and before any gate: it asks the
  active resolver about the submission's native subject in a worker thread,
  under a time budget, and checks the answer; when there is no resolver, no
  native subject, no match, an error, a timeout, or an answer the ledger
  cannot use, it records the passthrough subject, unresolved, with the
  reason. It never raises for the resolver's sake: a decision is never
  dropped over its identity. ``lookup`` serves the read path
  (``GET /api/v1/identity/resolve``).

A misconfigured deployment (an unknown resolver, a malformed plugin, a
``build()`` that returns something other than a synchronous
``IdentityResolver``) raises ``ResolverPluginError`` at startup. An
exception ``build()`` itself raises is not wrapped: it propagates as itself,
and also stops startup.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata as metadata
import inspect
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog
from pydantic import ValidationError

from app.identity.passthrough import (
    NO_NATIVE_SUBJECT,
    NO_RESOLVER,
    PASSTHROUGH,
    declared_identity,
    passthrough_identity,
)
from app.monitoring import duration_ms
from core.exceptions import ResolverPluginError
from core.identity import IdentityResolver, NativeSubject, ResolvedIdentity
from core.submission import storage_problem

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from importlib.metadata import EntryPoint

    from app.plugin import FrameworkServices
    from core.identity import NativeNamespace
    from core.observation import ReasonerRegistration
    from core.submission import Submission

log = structlog.get_logger(__name__)

#: Entry point group every identity resolver plugin registers under.
RESOLVER_GROUP = "decisionledger.identity_resolvers"

#: Wall-clock budget of one resolver call, in milliseconds. Resolvers are
#: synchronous in-process lookups (a versioned crosswalk), well under a
#: millisecond; the budget bounds a hung one, and the subject is then
#: recorded unresolved (DR-5: identity is not allowed to eat the decision's
#: latency budget).
DEFAULT_IDENTITY_BUDGET_MS = 50


@runtime_checkable
class IdentityResolverPlugin(Protocol):
    """The object a resolver publishes through the resolver entry-point group.

    Attributes:
        resolver_id: Stable identifier: the entry-point name,
            ``DECISIONLEDGER_IDENTITY_RESOLVER``'s value, and the
            ``resolved_by`` of every identity the resolver returns. Never
            ``passthrough``.
        relationship_types: Every relationship type the resolver emits in
            ``ResolvedIdentity.related``. An identity carrying any other is
            refused (the decision's subject is then recorded unresolved).
    """

    resolver_id: str
    relationship_types: frozenset[str]

    def build(self, framework: FrameworkServices) -> IdentityResolver:
        """Build the runtime resolver (load the crosswalk, open clients).

        Args:
            framework: The shared services (settings, Postgres, the store,
                the registry). Gates are not built yet.

        Returns:
            The runtime resolver. If it defines ``close()``, the framework
            calls it on shutdown.
        """
        ...


@dataclass(frozen=True)
class ComposedResolver:
    """A built resolver together with the plugin that declared it.

    Args:
        plugin: The plugin (its id and relationship types).
        resolver: The runtime resolver ``plugin.build`` returned.
    """

    plugin: IdentityResolverPlugin
    resolver: IdentityResolver

    @property
    def resolver_id(self) -> str:
        """The resolver's identifier (from its plugin)."""
        return self.plugin.resolver_id

    @property
    def relationship_types(self) -> frozenset[str]:
        """The relationship types the resolver declares."""
        return self.plugin.relationship_types

    def close(self) -> None:
        """Release the resolver's resources, if it holds any."""
        close = getattr(self.resolver, "close", None)
        if callable(close):
            close()


def check_resolver_plugin(obj: object, *, name: str) -> IdentityResolverPlugin:
    """Validate that ``obj`` is a well-formed resolver plugin named ``name``.

    Args:
        obj: The candidate plugin object.
        name: The entry-point name (or the id an explicit plugin is
            selected by).

    Returns:
        ``obj``, typed as an ``IdentityResolverPlugin``.

    Raises:
        ResolverPluginError: The object is not a well-formed plugin: it does
            not satisfy the protocol, its ``resolver_id`` differs from
            ``name`` or is ``passthrough``, or its ``relationship_types`` is
            not a frozenset of non-empty strings the ledger can store.
    """
    if not isinstance(obj, IdentityResolverPlugin):
        raise ResolverPluginError(
            name,
            f"object {obj!r} does not satisfy the IdentityResolverPlugin protocol "
            "(resolver_id, relationship_types, build)",
        )
    if obj.resolver_id != name:
        raise ResolverPluginError(
            name, f"entry point name must equal resolver_id ({obj.resolver_id!r})"
        )
    if obj.resolver_id == PASSTHROUGH:
        raise ResolverPluginError(
            name, f"{PASSTHROUGH!r} is the ledger's own default, not a plugin id"
        )
    types = obj.relationship_types
    if not isinstance(types, frozenset) or not all(
        isinstance(t, str) and t and storage_problem(t) is None for t in types
    ):
        raise ResolverPluginError(
            name,
            "relationship_types must be a frozenset of non-empty strings (the "
            "types the resolver emits; empty when it emits none)",
        )
    return obj


def select_resolver(
    resolver_id: str | None,
    *,
    plugins: Sequence[IdentityResolverPlugin] | None = None,
    entry_points: Iterable[EntryPoint] | None = None,
) -> IdentityResolverPlugin | None:
    """Find the deployment's active resolver plugin.

    Args:
        resolver_id: ``DECISIONLEDGER_IDENTITY_RESOLVER``, parsed:
            ``None`` keeps the passthrough default.
        plugins: Explicit plugins to choose from instead of the installed
            entry points (tests).
        entry_points: Explicit entry points to load instead of querying
            ``importlib.metadata`` for ``RESOLVER_GROUP`` (tests).

    Returns:
        The selected plugin, or ``None`` for the passthrough. Only the
        selected entry point is loaded.

    Raises:
        ResolverPluginError: No plugin (or more than one) is published under
            ``resolver_id``, its entry point fails to load, or it is
            malformed.
    """
    if plugins is not None:
        if resolver_id is None:
            return None
        matching = [
            plugin
            for plugin in plugins
            if getattr(plugin, "resolver_id", None) == resolver_id
        ]
        if len(matching) != 1:
            raise ResolverPluginError(
                resolver_id,
                f"{len(matching)} of the given plugins are published under this "
                "resolver_id; exactly one must be",
            )
        return check_resolver_plugin(matching[0], name=resolver_id)

    eps = list(
        entry_points
        if entry_points is not None
        else metadata.entry_points(group=RESOLVER_GROUP)
    )
    installed = sorted(ep.name for ep in eps)
    if resolver_id is None:
        if installed:
            log.info(
                "identity.resolvers_inactive",
                component="identity",
                installed=installed,
                msg="DECISIONLEDGER_IDENTITY_RESOLVER is blank: passthrough",
            )
        return None
    matching_eps = [ep for ep in eps if ep.name == resolver_id]
    if len(matching_eps) != 1:
        raise ResolverPluginError(
            resolver_id,
            f"{len(matching_eps)} entry points are published under this name in "
            f"{RESOLVER_GROUP!r} (installed: {installed}); exactly one must be",
        )
    [ep] = matching_eps
    try:
        obj = ep.load()
    except Exception as err:
        raise ResolverPluginError(
            resolver_id, f"entry point {ep.value!r} failed to load: {err}"
        ) from err
    plugin = check_resolver_plugin(obj, name=resolver_id)
    log.info(
        "identity.resolver_loaded",
        component="identity",
        resolver_id=resolver_id,
        entry_point=ep.value,
        relationship_types=sorted(plugin.relationship_types),
    )
    return plugin


def build_resolver(
    plugin: IdentityResolverPlugin, framework: FrameworkServices
) -> ComposedResolver:
    """Build the active resolver against the shared services.

    Args:
        plugin: The selected plugin.
        framework: The shared services handed to ``plugin.build``.

    Returns:
        The composed resolver.

    Raises:
        ResolverPluginError: ``build`` returned something that is not an
            ``IdentityResolver`` with a synchronous ``resolve`` (the object
            is closed first). An exception ``build`` raises propagates.
    """
    resolver = plugin.build(framework)
    problem: str | None = None
    if not isinstance(resolver, IdentityResolver):
        problem = "does not satisfy the IdentityResolver protocol (resolve)"
    elif inspect.iscoroutinefunction(resolver.resolve):
        problem = "has a coroutine resolve(); resolvers are synchronous"
    if problem is not None:
        with contextlib.suppress(Exception):
            close = getattr(resolver, "close", None)
            if callable(close):
                close()
        raise ResolverPluginError(
            plugin.resolver_id, f"build() returned {resolver!r}, which {problem}"
        )
    return ComposedResolver(plugin=plugin, resolver=resolver)


def identity_problem(
    identity: object,
    *,
    resolver_id: str,
    relationship_types: frozenset[str],
    native: NativeSubject,
) -> str | None:
    """Describe why a resolver's answer is unusable, or return ``None``.

    The ledger stores the identity verbatim in the bundle and serves it on
    the read path, so it must be exactly the contract type, valid (its id
    follows from its derivation, recomputed on reload), storable, a match
    (``deterministic`` or ``probabilistic``: an unresolved subject is the
    ledger's to record), and about the subject asked for, from this
    resolver, with only relationship types the resolver declares.
    """
    if not isinstance(identity, ResolvedIdentity) or type(identity) is not (
        ResolvedIdentity
    ):
        return (
            f"resolve() returned {type(identity).__name__}, not the contract's "
            "ResolvedIdentity"
        )
    try:
        problem = storage_problem(identity.model_dump(mode="json", warnings="error"))
        if problem is None:
            reloaded = ResolvedIdentity.model_validate_json(
                identity.model_dump_json(warnings="error")
            )
            if reloaded != identity:
                problem = "a value that does not survive the JSON round trip"
    except ValueError as err:  # ValidationError or a serialization error
        problem = f"a value the contract does not accept ({type(err).__name__})"
    if problem is not None:
        return f"it holds {problem}"
    provenance = identity.provenance
    if provenance.method == "unresolved":
        return (
            "it is unresolved; a resolver returns a match or None, and the "
            "ledger records an unmatched subject itself"
        )
    if provenance.resolved_by != resolver_id:
        return f"its provenance names {provenance.resolved_by!r} in resolved_by"
    asked = (native.native_namespace, native.native_subject_id)
    if (provenance.native_namespace, provenance.native_subject_id) != asked:
        return "its provenance names a different native subject than the one asked"
    undeclared = sorted(
        {r.relationship_type for r in identity.related} - relationship_types
    )
    if undeclared:
        return (
            f"it relates the subject by undeclared relationship type(s) "
            f"{undeclared} (declared: {sorted(relationship_types)})"
        )
    return None


@dataclass(frozen=True)
class IdentityLookup:
    """The read path's answer for one native subject.

    Args:
        identity: The subject, or ``None`` when the active resolver does not
            match it and no registration declares its namespace.
        reason: Why ``identity`` is unresolved or missing; ``None`` when the
            resolver matched it.
    """

    identity: ResolvedIdentity | None
    reason: str | None = None


class IdentityService:
    """The ledger's identity step: the active resolver, bounded, with fallback.

    Args:
        resolver: The active resolver, or ``None`` for the passthrough.
        budget_ms: Wall-clock budget of one resolver call.
    """

    def __init__(
        self,
        resolver: ComposedResolver | None = None,
        *,
        budget_ms: int = DEFAULT_IDENTITY_BUDGET_MS,
    ) -> None:
        """Bind the service to the deployment's resolver."""
        if budget_ms <= 0:
            msg = "the identity budget is a positive number of milliseconds"
            raise ValueError(msg)
        self._resolver = resolver
        self._budget_ms = budget_ms

    @property
    def resolver_id(self) -> str:
        """The active resolver's id, or ``passthrough``."""
        return self._resolver.resolver_id if self._resolver else PASSTHROUGH

    @property
    def relationship_types(self) -> frozenset[str]:
        """The relationship types the active resolver declares (none: passthrough)."""
        return self._resolver.relationship_types if self._resolver else frozenset()

    async def identify(
        self,
        submission: Submission,
        registration: ReasonerRegistration,
        *,
        decision_id: str | None = None,
    ) -> ResolvedIdentity:
        """Establish a decision's subject. Never raises for the resolver's sake.

        Args:
            submission: The validated submission.
            registration: Its reasoner's registration (the passthrough reads
                its native namespace declarations).
            decision_id: The decision, for log correlation.

        Returns:
            The active resolver's identity for the submission's native
            subject when it matched it and the answer is usable; otherwise
            the passthrough subject, unresolved, with the reason.
        """
        native = submission.native_subject
        if native is None:
            return passthrough_identity(
                submission,
                registration,
                reason=NO_NATIVE_SUBJECT,
                resolved_at=datetime.now(UTC),
            )
        outcome = await self._consult(
            native,
            log_ctx={
                "event_id": submission.event_id,
                "decision_id": decision_id,
                "reasoner_id": submission.reasoner_id,
            },
        )
        if isinstance(outcome, ResolvedIdentity):
            return outcome
        return passthrough_identity(
            submission,
            registration,
            reason=outcome,
            resolved_at=datetime.now(UTC),
        )

    async def lookup(
        self,
        native: NativeSubject,
        *,
        namespaces: Mapping[str, NativeNamespace],
    ) -> IdentityLookup:
        """Resolve a native subject outside a decision (the read path).

        Args:
            native: The native subject.
            namespaces: The deployment's native namespace declarations, for
                the passthrough.

        Returns:
            The resolver's match; else the passthrough subject of a declared
            namespace, unresolved; else no identity, with the reason.
        """
        outcome = await self._consult(native, log_ctx={})
        if isinstance(outcome, ResolvedIdentity):
            return IdentityLookup(identity=outcome)
        namespace = namespaces.get(native.native_namespace)
        if namespace is None:
            return IdentityLookup(identity=None, reason=outcome)
        identity = declared_identity(
            namespace, native, reason=outcome, resolved_at=datetime.now(UTC)
        )
        return IdentityLookup(identity=identity, reason=outcome)

    async def _consult(
        self, native: NativeSubject, *, log_ctx: dict[str, str | None]
    ) -> ResolvedIdentity | str:
        """The active resolver's usable answer, or why there is none."""
        composed = self._resolver
        if composed is None:
            return NO_RESOLVER
        resolver_id = composed.resolver_id
        budget_ms = self._budget_ms
        deadline = asyncio.timeout(budget_ms / 1_000)
        t0 = time.perf_counter()
        try:
            async with deadline:
                answer = await asyncio.to_thread(
                    composed.resolver.resolve,
                    native.native_namespace,
                    native.native_subject_id,
                )
        except TimeoutError as err:
            if deadline.expired():
                reason = f"resolver {resolver_id!r} timed out after {budget_ms} ms"
            else:
                reason = f"resolver {resolver_id!r} raised TimeoutError: {err}"
            return self._fallback(reason, resolver_id, t0, log_ctx)
        except asyncio.CancelledError as err:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise  # the caller is being cancelled
            reason = f"resolver {resolver_id!r} raised CancelledError: {err}"
            return self._fallback(reason, resolver_id, t0, log_ctx)
        except Exception as err:
            reason = f"resolver {resolver_id!r} raised {type(err).__name__}: {err}"
            return self._fallback(reason, resolver_id, t0, log_ctx)
        if answer is None:
            return self._fallback(
                f"resolver {resolver_id!r} found no match", resolver_id, t0, log_ctx
            )
        problem = identity_problem(
            answer,
            resolver_id=resolver_id,
            relationship_types=composed.relationship_types,
            native=native,
        )
        if problem is not None:
            reason = (
                f"resolver {resolver_id!r} returned an unusable identity: {problem}"
            )
            return self._fallback(reason, resolver_id, t0, log_ctx)
        return answer

    @staticmethod
    def _fallback(
        reason: str,
        resolver_id: str,
        t0: float,
        log_ctx: Mapping[str, str | None],
    ) -> str:
        """Log why the resolver's answer is not used; return the storable reason."""
        reason = _storable_text(reason)
        log.warning(
            "identity.unresolved",
            component="identity",
            resolver_id=resolver_id,
            reason=reason,
            duration_ms=duration_ms(t0),
            **{key: value for key, value in log_ctx.items() if value is not None},
        )
        return reason

    def close(self) -> None:
        """Release the active resolver's resources."""
        if self._resolver is not None:
            self._resolver.close()


def _storable_text(text: str) -> str:
    """Escape what the ledger cannot store: NUL and unencodable characters."""
    escaped = text.encode("utf-8", "backslashreplace").decode("utf-8")
    return escaped.replace("\x00", "\\x00")


def native_subject(native_namespace: str, native_subject_id: str) -> NativeSubject:
    """A validated native subject from read-path parameters.

    Raises:
        ValueError: A parameter is empty or not storable.
    """
    for value in (native_namespace, native_subject_id):
        problem = storage_problem(value)
        if problem is not None:
            msg = f"the native subject holds {problem}"
            raise ValueError(msg)
    try:
        return NativeSubject(
            native_namespace=native_namespace, native_subject_id=native_subject_id
        )
    except ValidationError as err:
        msg = "a native subject is a non-empty namespace and a non-empty id"
        raise ValueError(msg) from err


__all__ = [
    "DEFAULT_IDENTITY_BUDGET_MS",
    "RESOLVER_GROUP",
    "ComposedResolver",
    "IdentityLookup",
    "IdentityResolverPlugin",
    "IdentityService",
    "build_resolver",
    "check_resolver_plugin",
    "identity_problem",
    "native_subject",
    "select_resolver",
]
