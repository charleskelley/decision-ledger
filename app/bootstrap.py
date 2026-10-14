"""Framework bootstrap — the single place the shared services are built.

``build_framework_services`` turns a ``FrameworkSettings``, the discovered
reasoner plugins, and the discovered gate plugins into one
``FrameworkServices`` object: the Postgres connection (with the canonical
DDL applied), the bundle store, the reasoner registry, the composed reasoner
plugins, and the composed gates. The FastAPI lifespan (``app/main.py``) and
the eval harness drivers both call it, so there is exactly one description
of how the framework is wired.

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

from app.audit.schema import apply_canonical_schema
from app.audit.store import BundleStore
from app.gates import build_gates, discover_gates
from app.plugin import FrameworkServices
from app.plugins import build_registry

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.gates import GatePlugin
    from app.plugin import ReasonerPlugin
    from app.settings import FrameworkSettings

log = structlog.get_logger(__name__)


def build_framework_services(
    settings: FrameworkSettings,
    plugins: Sequence[ReasonerPlugin],
    gate_plugins: Sequence[GatePlugin] | None = None,
) -> FrameworkServices:
    """Construct every shared service the framework runs on.

    Applies the canonical DDL to Postgres before building anything else so
    the store and the journal can rely on a fresh schema. A gate that keeps
    tables in the same database applies its own DDL, in its own schema, when
    it is built. Gates are built last, against the services built so far; a
    gate that fails to build closes the gates built before it and the
    Postgres connection, then the error propagates.

    Args:
        settings: Connection parameters and credentials.
        plugins: The reasoner plugins this deployment composes. Their
            registrations form the reasoner registry and their raw-event
            factories are handed to the bundle store for replay.
        gate_plugins: The gate plugins this deployment composes. ``None``
            (the default) discovers every plugin installed under the
            ``decisionledger.gates`` entry-point group; tests pass an
            explicit list.

    Returns:
        A ``FrameworkServices`` whose ``close()`` closes the gates and then
        releases the Postgres connection.

    Raises:
        GatePluginError: A gate plugin is malformed or cannot be built.
    """
    composed_gate_plugins = (
        list(gate_plugins) if gate_plugins is not None else discover_gates()
    )

    pg_conn = psycopg.connect(settings.postgres_dsn)
    try:
        apply_canonical_schema(pg_conn)
        registry = build_registry(plugins)
        store = BundleStore(
            conn=pg_conn,
            raw_event_factories={
                p.registration.reasoner_id: p.raw_event_factory for p in plugins
            },
            registry=registry,
        )
        services = FrameworkServices(
            settings=settings,
            pg_conn=pg_conn,
            store=store,
            registry=registry,
            reasoners=tuple(plugins),
        )
        gates = build_gates(composed_gate_plugins, services)
    except Exception:
        pg_conn.close()
        raise

    log.info(
        "framework.bootstrap_complete",
        component="bootstrap",
        reasoner_ids=[p.registration.reasoner_id for p in plugins],
        gate_ids=list(gates),
    )
    return dataclasses.replace(services, gates=MappingProxyType(gates))
