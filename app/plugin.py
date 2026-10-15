"""Reasoner plugin seam — the contract between the framework and a reasoner.

A reasoner joins a DecisionLedger deployment by publishing one object that
satisfies ``ReasonerPlugin`` through the ``decisionledger.reasoners`` entry
point group (see ``app/plugins.py``). The framework never imports a reasoner
package directly, and since DR-31 it never runs a reasoner's code either: a
plugin publishes the reasoner's registration and the assets its gates read,
and the reasoner itself runs as its own service (calling the ledger over
HTTP) or in process through ``app.client.LocalClient``. A reasoner with no
plugin installed registers through the deployment's registration file
instead (``DECISIONLEDGER_REGISTRATIONS``).

Two types make up the seam:

- ``FrameworkServices`` — the shared infrastructure and framework components
  built once per process by ``app/bootstrap.py:build_framework_services``:
  Postgres, the bundle store, the resolution journal, the reasoner registry
  (every registration, plugin or file), the composed reasoner plugins, and
  the composed gates (``app/gates.py``). Gates own their own infrastructure
  (DR-28), so nothing gate-specific lives here. ``LocalClient`` and the HTTP
  routes both operate on it.
- ``ReasonerPlugin`` — the plugin object itself: the registration record and
  the prompt and corpus directories a gate may read. Replay needs no
  reasoner code: a bundle stores the reasoner's ``Submission``, a concrete
  framework type, with its own event as opaque JSON, and the ledger keeps a
  snapshot of every registration version a bundle cites (DR-31).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from app.gates import close_gates

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    import psycopg

    from app.audit.resolution_journal import ResolutionJournal
    from app.audit.store import BundleStore
    from app.gates import ComposedGate
    from app.reasoner_registry import StaticReasonerRegistry
    from app.settings import FrameworkSettings
    from core.observation import ReasonerRegistration


@dataclass(frozen=True)
class FrameworkServices:
    """Shared infrastructure and framework components, built once per process.

    Gate plugins receive this object in ``build`` and may hold a reference
    to it; the HTTP routes and ``LocalClient`` run every ledger operation
    against it. The framework owns the lifecycle and calls ``close()`` on
    shutdown.

    Args:
        settings: The framework settings the services were built from.
        pg_conn: Open psycopg connection the bundle store writes through.
        store: Decision-bundle persistence and replay.
        registry: Every registration this deployment serves (installed
            plugins and the registration file), with versions and sources.
        reasoners: The composed reasoner plugins, in composition order.
            Gates read reasoner-shipped assets from them (a retrieving gate
            loads each reasoner's ``prompts_dir`` and ``corpus_dir``). A
            reasoner registered by file has no plugin and no assets here.
        gates: Composed gates keyed by ``gate_id`` (each flagged when the
            deployment mandates it). Empty while the gates themselves are
            being built: a gate never reaches another gate.
        journal: The append-only resolution journal (human resolutions of
            held decisions), or ``None`` when the services were assembled
            without one (a test double).
    """

    settings: FrameworkSettings
    pg_conn: psycopg.Connection[Any]
    store: BundleStore
    registry: StaticReasonerRegistry
    reasoners: tuple[ReasonerPlugin, ...] = ()
    gates: Mapping[str, ComposedGate] = field(default_factory=dict)
    journal: ResolutionJournal | None = None

    def close(self) -> None:
        """Close every composed gate, then the framework's own connection.

        The connection is closed even when a gate's ``close()`` raises; the
        gate error then propagates.
        """
        try:
            close_gates(self.gates)
        finally:
            self.pg_conn.close()


@runtime_checkable
class ReasonerPlugin(Protocol):
    """The object a reasoner publishes through the entry-point group.

    Attributes:
        registration: The reasoner's ``ReasonerRegistration`` — identity,
            gate set, bypass rules, and enforcement profile. Its
            ``reasoner_id`` must equal the entry-point name.
        prompts_dir: Directory of ``*.yaml`` prompt templates an LLM-backed
            gate loads for this reasoner.
        corpus_dir: Directory of Markdown policy documents a retrieving gate
            indexes for this reasoner.
    """

    registration: ReasonerRegistration
    prompts_dir: Path
    corpus_dir: Path
