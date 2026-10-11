"""Reasoner plugin seam — the contract between the framework and a reasoner.

A reasoner joins a DecisionLedger deployment by publishing one object that
satisfies ``ReasonerPlugin`` through the ``decisionledger.reasoners`` entry
point group (see ``app/plugins.py``). The framework never imports a reasoner
package directly: it discovers plugins, builds the shared infrastructure once
(``FrameworkServices``), then asks each plugin to build its own services
against that shared set.

Three types make up the seam:

- ``FrameworkServices`` — the shared infrastructure clients and framework
  components (Postgres, Elasticsearch, retriever, prompt registry, policy
  gate, bundle store, reasoner registry) built once per process by
  ``app/bootstrap.py:build_framework_services``.
- ``ReasonerServices`` — whatever a reasoner needs at request time (feature
  stores, ML scorers, its own queues). The framework only knows how to
  ``close()`` it.
- ``ReasonerPlugin`` — the plugin object itself: the registration record,
  the FastAPI router, the prompt and corpus directories, the raw-event
  deserializer used by replay, and the ``build_services`` factory.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

    import psycopg
    from elasticsearch import Elasticsearch
    from fastapi import APIRouter

    from app.audit.store import BundleStore
    from app.gate.policy.gate import PolicyGate
    from app.retrieval.retriever import PolicyRetriever
    from app.settings import FrameworkSettings
    from core.gate.policy import PromptRegistry
    from core.observation import ReasonerRegistration, ReasonerRegistry

#: Deserializer signature for a reasoner's raw event payload. The bundle store
#: calls this on the JSON dict stored in ``bundle["raw_event"]`` to reconstitute
#: a typed Observation for replay. The return is typed loosely as ``Any``
#: because Pydantic ``computed_field`` properties make concrete event classes
#: structurally — but not nominally — compatible with the ``Observation``
#: Protocol; downstream code duck-types on the protocol.
RawEventFactory = Callable[[dict[str, Any]], Any]


@dataclass(frozen=True)
class FrameworkServices:
    """Shared infrastructure and framework components, built once per process.

    Reasoner plugins receive this object in ``build_services`` and may hold a
    reference to it; the framework owns the lifecycle and calls ``close()``
    on shutdown after every reasoner's services have been closed.

    Args:
        settings: The framework settings the services were built from.
        pg_conn: Open psycopg connection (pgvector adapters registered).
        es: Elasticsearch client for the BM25 leg of retrieval.
        retriever: Hybrid policy retriever (pgvector + BM25 + reranker).
        prompt_registry: Prompt-template registry the policy gate renders from.
        gate: LLM policy gate.
        store: Decision-bundle persistence and replay.
        registry: Registry of every reasoner this deployment composes.
    """

    settings: FrameworkSettings
    pg_conn: psycopg.Connection[Any]
    es: Elasticsearch
    retriever: PolicyRetriever
    prompt_registry: PromptRegistry
    gate: PolicyGate
    store: BundleStore
    registry: ReasonerRegistry

    def close(self) -> None:
        """Release the infrastructure connections this object owns."""
        self.pg_conn.close()
        self.es.close()


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
            allowed gates/templates/jurisdictions, and enforcement profile.
            Its ``reasoner_id`` must equal the entry-point name.
        router: FastAPI router mounted by ``create_app``.
        prompts_dir: Directory of ``*.yaml`` prompt templates the gate loads.
        corpus_dir: Directory of Markdown policy documents the corpus loader
            indexes for this reasoner.
        raw_event_factory: Deserializer from the stored ``raw_event`` JSON
            dict back to the reasoner's typed Observation, used by replay.
    """

    registration: ReasonerRegistration
    router: APIRouter
    prompts_dir: Path
    corpus_dir: Path
    raw_event_factory: RawEventFactory

    def build_services(self, framework: FrameworkServices) -> ReasonerServices:
        """Build this reasoner's request-time services against shared infra.

        Args:
            framework: The shared services built by the framework bootstrap.

        Returns:
            The reasoner's services object; the framework closes it on
            shutdown.
        """
        ...
