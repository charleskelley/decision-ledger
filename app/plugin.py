"""Reasoner plugin seam — the contract between the framework and a reasoner.

A reasoner joins a DecisionLedger deployment by publishing one object that
satisfies ``ReasonerPlugin`` through the ``decisionledger.reasoners`` entry
point group (see ``app/plugins.py``). The framework never imports a reasoner
package directly, and since DR-31 it never runs a reasoner's code either: a
plugin publishes the reasoner's registration and the assets its gates read,
and the reasoner itself runs as its own service (calling the ledger over
HTTP) or in process through ``app.client.LocalClient``. A reasoner with no
plugin installed registers through the deployment's registration file
(``DECISIONLEDGER_REGISTRATIONS``) or over the API
(``PUT /api/v1/reasoners/{reasoner_id}``, DR-33) instead.

Two types make up the seam:

- ``FrameworkServices`` — the shared infrastructure and framework components
  built once per process by ``app/bootstrap.py:build_framework_services``:
  Postgres, the bundle store, the resolution journal, the live reasoner
  registry (every registration: plugin, file, or API), the composed reasoner
  plugins, the composed gates (``app/gates.py``), and the identity step with
  the deployment's active resolver (``app/identity``, DR-32). Gates own their
  own infrastructure (DR-28), so nothing gate-specific lives here.
  ``LocalClient`` and the HTTP routes both operate on it.
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
from app.identity import IdentityService

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    import psycopg

    from app.audit.resolution_journal import ResolutionJournal
    from app.audit.store import BundleStore
    from app.gates import ComposedGate
    from app.reasoner_registry import LiveReasonerRegistry
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
            plugins, the registration file, and the registrations written
            over the API), with versions and sources; the registrations
            resource writes to it.
        reasoners: The composed reasoner plugins, in composition order.
            Gates read reasoner-shipped assets from them (a retrieving gate
            loads each reasoner's ``prompts_dir`` and ``corpus_dir``). A
            reasoner registered by file or over the API has no plugin and no
            assets here: a gate that needs its assets reads them from where
            the gate documents, when it admits the reasoner's binding.
        gates: Composed gates keyed by ``gate_id`` (each flagged when the
            deployment mandates it). Empty while the gates themselves are
            being built: a gate never reaches another gate.
        journal: The append-only resolution journal (human resolutions of
            held decisions), or ``None`` when the services were assembled
            without one (a test double).
        identity: The identity step: the deployment's active resolver
            (``DECISIONLEDGER_IDENTITY_RESOLVER``) with its budget and the
            passthrough fallback. The passthrough alone by default.
    """

    settings: FrameworkSettings
    pg_conn: psycopg.Connection[Any]
    store: BundleStore
    registry: LiveReasonerRegistry
    reasoners: tuple[ReasonerPlugin, ...] = ()
    gates: Mapping[str, ComposedGate] = field(default_factory=dict)
    journal: ResolutionJournal | None = None
    identity: IdentityService = field(default_factory=IdentityService)

    def close(self) -> None:
        """Close every composed gate, the resolver, then the connection.

        The resolver and the connection are closed even when a gate's
        ``close()`` raises, and the connection even when the resolver's does;
        the first error then propagates.
        """
        try:
            close_gates(self.gates)
        finally:
            try:
                self.identity.close()
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
