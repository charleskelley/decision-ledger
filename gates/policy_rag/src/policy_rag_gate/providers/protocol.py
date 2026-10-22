"""The retrieval provider seam — gate-internal (DR-34).

The policy gate never talks to a search engine directly: it asks a
``RetrievalProvider`` per corpus a binding names. A provider answers two
questions — what state is this corpus in (``describe``) and what passages
answer this query (``retrieve``) — and its answer to the second is a
``ProviderResult``, whose ``RetrievedContext`` items are the evidence
contract every provider must meet: a provider cannot hand the gate a
passage without its corpus, tier, document, provenance level, and the
SHA-256 of its text.

Providers are plugins of the gate, not of the framework: they are
discovered under the gate-owned entry-point group
``policy_rag_gate.providers`` (``policy_rag_gate.providers``), each target
a ``ProviderPlugin`` that builds its provider from ``ProviderServices``.
The reference provider (pgvector + Elasticsearch + cross-encoder, loaded by
the corpus loader) ships in this member as ``reference``. Reasoners never
import any of this: they name corpora in their registration's binding.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from policy_rag_gate.corpus import CorpusRef, CorpusState
from policy_rag_gate.models.context import RetrievedContext

if TYPE_CHECKING:
    from collections.abc import Sequence

    from policy_rag_gate.settings import PolicyRagGateSettings


class ProviderContractError(RuntimeError):
    """A provider's answer breaks the evidence contract for the corpus asked.

    Raised by the gate when a result's passages name another corpus,
    provider, or tier than the reference it queried, or fall below the
    binding's minimum provenance. Like any retrieval error it fails the
    gate closed (the framework records the gate as failed and enforcement
    holds the decision).
    """


class ProviderResult(BaseModel):
    """A provider's answer to one query against one corpus.

    Attributes:
        state: The corpus's state when it was queried.
        contexts: The passages, best first, ranked within this corpus only
            (``rank`` 1, 2, ...).
        ranking_requested: The ranking the provider attempted (the
            reference provider: ``"reranked"`` with a cross-encoder,
            ``"rrf_only"`` without one).
        ranking_applied: The ranking that produced ``contexts`` (the
            reference provider falls back to ``"rrf_only"`` when reranking
            exceeds its latency budget).
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    state: CorpusState
    contexts: tuple[RetrievedContext, ...]
    ranking_requested: str
    ranking_applied: str


@runtime_checkable
class RetrievalProvider(Protocol):
    """A source of passages for the corpora it holds.

    Attributes:
        provider_id: The id bindings name it by (``CorpusRef.provider``).
    """

    @property
    def provider_id(self) -> str:
        """The id a ``CorpusRef`` names this provider by."""
        ...

    def describe(self, corpus: CorpusRef) -> CorpusState:
        """Report the corpus's current state.

        Args:
            corpus: The reference, as a binding names it.

        Returns:
            Its state marker, the provenance the provider supplies, and the
            provider's version.

        Raises:
            Exception: The provider does not hold the corpus or cannot be
                reached. The gate refuses a binding whose corpus it cannot
                describe.
        """
        ...

    def retrieve(
        self,
        query: str,
        corpus: CorpusRef,
        *,
        k: int,
        jurisdictions: Sequence[str] | None,
        risk_tier: str | None,
    ) -> ProviderResult:
        """Retrieve at most ``k`` passages for ``query`` from one corpus.

        Args:
            query: The reasoner-rendered retrieval query.
            corpus: The corpus to search (its tier is stamped on every
                passage).
            k: Most passages to return.
            jurisdictions: Jurisdiction filter, or ``None`` for none.
            risk_tier: Risk-tier filter, or ``None`` for none (a provider
                that cannot filter by it ignores it).

        Returns:
            The passages and the state of the corpus they came from.
        """
        ...


@dataclass(frozen=True)
class ProviderServices:
    """What a provider plugin may build its provider from.

    Attributes:
        postgres_dsn: DSN of the framework's database (the reference
            provider keeps the ``policy_rag`` schema there).
        settings: The gate's settings (``POLICY_RAG_GATE_*``).
    """

    postgres_dsn: str
    settings: PolicyRagGateSettings


class ProviderPlugin(Protocol):
    """The target of a ``policy_rag_gate.providers`` entry point.

    Attributes:
        provider_id: Must equal the entry point's name.
    """

    provider_id: str

    def build(self, services: ProviderServices) -> RetrievalProvider:
        """Open whatever the provider needs and return it.

        Args:
            services: The gate's settings and the framework's DSN.

        Returns:
            The provider; when it has a ``close()``, the gate calls it on
            shutdown.
        """
        ...
