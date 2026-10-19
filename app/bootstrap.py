"""Framework bootstrap — the single place the shared services are built.

``build_framework_services`` turns a ``FrameworkSettings``, the discovered
reasoner plugins, and the discovered gate plugins into one
``FrameworkServices`` object: the Postgres connection (with the canonical
DDL applied), the bundle store, the resolution journal, the reasoner registry
(the plugins' registrations plus the registration file's, each version
snapshotted before the first decision), the composed reasoner plugins, the
identity step with the deployment's active resolver
(``DECISIONLEDGER_IDENTITY_RESOLVER``, DR-32), and the composed gates (the
ones ``DECISIONLEDGER_MANDATORY_GATES`` names flagged mandatory). The FastAPI
lifespan (``app/main.py``), ``LocalClient``, and the eval harness drivers all
call it, so there is exactly one description of how the framework is wired.

The framework's own infrastructure is Postgres. Each gate builds whatever it
needs itself (``GatePlugin.build``, DR-28): a retrieving gate opens its own
index and model clients, a rules gate opens nothing.
"""

from __future__ import annotations

import dataclasses
from types import MappingProxyType
from typing import TYPE_CHECKING

import psycopg
import structlog

from app.audit.resolution_journal import ResolutionJournal
from app.audit.schema import apply_canonical_schema
from app.audit.store import BundleStore
from app.gates import build_gates, discover_gates
from app.identity import (
    IdentityService,
    build_resolver,
    declared_namespaces,
    select_resolver,
)
from app.plugin import FrameworkServices
from app.plugins import build_registry

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.gates import GatePlugin
    from app.identity import ComposedResolver, IdentityResolverPlugin
    from app.plugin import ReasonerPlugin
    from app.settings import FrameworkSettings

log = structlog.get_logger(__name__)


def build_framework_services(
    settings: FrameworkSettings,
    plugins: Sequence[ReasonerPlugin],
    gate_plugins: Sequence[GatePlugin] | None = None,
    *,
    resolver_plugins: Sequence[IdentityResolverPlugin] | None = None,
) -> FrameworkServices:
    """Construct every shared service the framework runs on.

    Applies the canonical DDL to Postgres before building anything else so
    the store and the journal can rely on a fresh schema, then records a
    snapshot of every registration the deployment serves (a version already
    recorded is left as it is), so every bundle's ``registration_version``
    has one. A gate that keeps tables in the same database applies its own
    DDL, in its own schema, when it is built. The active identity resolver
    is built next, then the gates, each against the services built so far;
    a resolver or gate that fails to build closes what was built before it
    and the Postgres connection, then the error propagates.

    Args:
        settings: Connection parameters and credentials, and the
            registration file (``DECISIONLEDGER_REGISTRATIONS``).
        plugins: The reasoner plugins this deployment composes. Their
            registrations, with the registration file's, form the reasoner
            registry intake and replay read.
        gate_plugins: The gate plugins this deployment composes. ``None``
            (the default) discovers every plugin installed under the
            ``decisionledger.gates`` entry-point group; tests pass an
            explicit list.
        resolver_plugins: The identity resolver plugins to choose the active
            one from (``DECISIONLEDGER_IDENTITY_RESOLVER`` names it). ``None``
            (the default) loads it from the plugins installed under the
            ``decisionledger.identity_resolvers`` entry-point group; tests
            pass an explicit list.

    Returns:
        A ``FrameworkServices`` whose ``close()`` closes the gates and then
        releases the Postgres connection.

    Raises:
        GatePluginError: A gate plugin is malformed, its ``build`` returns
            something that does not satisfy ``Gate``, or
            ``DECISIONLEDGER_MANDATORY_GATES`` names a gate that is not
            composed. An exception ``build`` raises propagates as itself.
        ResolverPluginError: ``DECISIONLEDGER_IDENTITY_RESOLVER`` names a
            resolver that is not installed, whose plugin is malformed, or
            whose ``build`` returns something other than a synchronous
            ``IdentityResolver``. An exception ``build`` raises propagates
            as itself.
        RegistrationError: The registration file cannot be loaded, registers
            a reasoner twice, or two registrations declare one native
            namespace differently.
    """
    composed_gate_plugins = (
        list(gate_plugins) if gate_plugins is not None else discover_gates()
    )
    resolver_plugin = select_resolver(
        settings.identity_resolver_id, plugins=resolver_plugins
    )

    # Autocommit: each store method opens its own transaction block, so no
    # read is left idle in a transaction and a failed write cannot leave the
    # shared connection aborted.
    pg_conn = psycopg.connect(settings.postgres_dsn, autocommit=True)
    resolver: ComposedResolver | None = None
    try:
        apply_canonical_schema(pg_conn)
        registry = build_registry(
            plugins, registration_file=settings.registrations_path
        )
        declared_namespaces(entry.registration for entry in registry.entries())
        store = BundleStore(conn=pg_conn, registry=registry)
        for entry in registry.entries():
            store.snapshots.record(entry.registration)
        services = FrameworkServices(
            settings=settings,
            pg_conn=pg_conn,
            store=store,
            registry=registry,
            reasoners=tuple(plugins),
            journal=ResolutionJournal(pg_conn),
        )
        if resolver_plugin is not None:
            resolver = build_resolver(resolver_plugin, services)
        services = dataclasses.replace(services, identity=IdentityService(resolver))
        gates = build_gates(
            composed_gate_plugins, services, mandatory=settings.mandatory_gate_ids
        )
    except Exception:
        try:
            if resolver is not None:
                resolver.close()
        finally:
            pg_conn.close()
        raise

    log.info(
        "framework.bootstrap_complete",
        component="bootstrap",
        reasoner_ids=[entry.reasoner_id for entry in registry.entries()],
        registration_sources={e.reasoner_id: e.source for e in registry.entries()},
        gate_ids=list(gates),
        mandatory_gate_ids=sorted(g for g, c in gates.items() if c.mandatory),
        identity_resolver=services.identity.resolver_id,
    )
    return dataclasses.replace(services, gates=MappingProxyType(gates))
