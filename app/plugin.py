"""Reasoner plugin seam — the contract between the framework and a reasoner.

A reasoner joins a DecisionLedger deployment by publishing one object that
satisfies ``ReasonerPlugin`` through the ``decisionledger.reasoners`` entry
point group (see ``app/plugins.py``). The framework never imports a reasoner
package directly: it discovers plugins, builds the shared infrastructure once
(``FrameworkServices``), then asks each plugin to build its own services
against that shared set.

Three types make up the seam:

- ``FrameworkServices`` — the shared infrastructure and framework components
  built once per process by ``app/bootstrap.py:build_framework_services``:
  Postgres, the bundle store, the reasoner registry, the composed reasoner
  plugins, and the composed gates (``app/gates.py``). Gates own their own
  infrastructure (DR-28), so nothing gate-specific lives here.
- ``ReasonerServices`` — whatever a reasoner needs at request time (feature
  stores, ML scorers, its own queues). The framework only knows how to
  ``close()`` it.
- ``ReasonerPlugin`` — the plugin object itself: the registration record,
  the FastAPI router, the prompt and corpus directories, and the
  ``build_services`` factory. Replay needs no reasoner code: a bundle stores
  the reasoner's ``Submission``, a concrete framework type, with its own
  event as opaque JSON (DR-31). It does need the reasoner's registration
  composed, for its enforcement profile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from app.gates import close_gates

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    import psycopg
    from fastapi import APIRouter

    from app.audit.store import BundleStore
    from app.gates import ComposedGate
    from app.settings import FrameworkSettings
    from core.observation import ReasonerRegistration, ReasonerRegistry


@dataclass(frozen=True)
class FrameworkServices:
    """Shared infrastructure and framework components, built once per process.

    Reasoner plugins receive this object in ``build_services`` and gate
    plugins in ``build``; either may hold a reference to it. The framework
    owns the lifecycle and calls ``close()`` on shutdown after every
    reasoner's services have been closed.

    Args:
        settings: The framework settings the services were built from.
        pg_conn: Open psycopg connection the bundle store writes through.
        store: Decision-bundle persistence and replay.
        registry: Registry of every reasoner this deployment composes.
        reasoners: The composed reasoner plugins, in composition order.
            Gates read reasoner-shipped assets from them (a retrieving gate
            loads each reasoner's ``prompts_dir`` and ``corpus_dir``).
        gates: Composed gates keyed by ``gate_id`` (each flagged when the
            deployment mandates it). Empty while the gates themselves are
            being built: a gate never reaches another gate.
    """

    settings: FrameworkSettings
    pg_conn: psycopg.Connection[Any]
    store: BundleStore
    registry: ReasonerRegistry
    reasoners: tuple[ReasonerPlugin, ...] = ()
    gates: Mapping[str, ComposedGate] = field(default_factory=dict)

    def close(self) -> None:
        """Close every composed gate, then the framework's own connection.

        The connection is closed even when a gate's ``close()`` raises; the
        gate error then propagates.
        """
        try:
            close_gates(self.gates)
        finally:
            self.pg_conn.close()


class ReasonerServices(Protocol):
    """Request-time services a reasoner builds for itself.

    The framework stores one instance per reasoner on ``app.state.reasoners``
    and only ever calls ``close()`` on it; the reasoner's own router and
    pipeline know the concrete type.
    """

    def close(self) -> None:
        """Release any connections or resources the reasoner opened."""
        ...


@runtime_checkable
class ReasonerPlugin(Protocol):
    """The object a reasoner publishes through the entry-point group.

    Attributes:
        registration: The reasoner's ``ReasonerRegistration`` — identity,
            gate set, bypass rules, and enforcement profile. Its
            ``reasoner_id`` must equal the entry-point name.
        router: FastAPI router mounted by ``create_app``.
        prompts_dir: Directory of ``*.yaml`` prompt templates an LLM-backed
            gate loads for this reasoner.
        corpus_dir: Directory of Markdown policy documents a retrieving gate
            indexes for this reasoner.
    """

    registration: ReasonerRegistration
    router: APIRouter
    prompts_dir: Path
    corpus_dir: Path

    def build_services(self, framework: FrameworkServices) -> ReasonerServices:
        """Build this reasoner's request-time services against shared infra.

        Args:
            framework: The shared services built by the framework bootstrap.

        Returns:
            The reasoner's services object; the framework closes it on
            shutdown.
        """
        ...
