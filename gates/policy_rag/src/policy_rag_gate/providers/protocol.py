"""The retrieval provider seam — gate-internal (DR-34).

The policy gate never talks to a search engine directly: it asks a
``RetrievalProvider`` per corpus a binding names. A provider answers two
questions — what state is this corpus in (``describe``) and what passages
answer this query (``retrieve``) — and its answer to the second is a
``ProviderResult``, whose ``RetrievedContext`` items are the evidence
contract every provider must meet: a provider cannot hand the gate a
passage without its corpus, tier, document, provenance level, and the
SHA-256 of its text.

A provider may also fetch whole documents (``DocumentFetcher``, DR-34 part
2): the gate escrows the documents its passages came from when it decides,
and compares a stored passage with the document, escrowed or as the
provider holds it now, when it verifies.

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

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from policy_rag_gate.corpus import CorpusRef, CorpusState
from policy_rag_gate.models.context import RetrievedContext, content_hash

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


class DocumentNotHeldError(LookupError):
    """The provider holds the corpus, but not that document version.

    Raised by ``DocumentFetcher.fetch_document``. When the gate verifies a
    passage live, it means the version the passage was retrieved from is
    gone while its corpus is still there: ``source_changed``. Any other
    error (the corpus not loaded, the store unreachable) is
    ``source_unavailable``.
    """


class DocumentBlob(BaseModel):
    """One whole document, as its provider holds it (DR-34 part 2).

    What ``DocumentFetcher.fetch_document`` returns. ``text`` is the text a
    passage's ``span`` indexes (the reference provider: the document's body
    as the corpus loader stored it, front matter stripped); the gate
    escrows it as UTF-8, so its escrow key is ``content_hash``.

    Attributes:
        corpus_id: The corpus it belongs to.
        document_id: The document.
        version: The version returned (``None`` when the provider does not
            version its documents).
        title: The document's title.
        media_type: The text's media type.
        text: The whole text.
        content_hash: SHA-256 (lowercase hex) of ``text`` as UTF-8; must
            match it.
        metadata: The provider's metadata for the document (the reference
            provider: its front matter).
    """

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", allow_inf_nan=False
    )

    corpus_id: str
    document_id: str
    version: str | None
    title: str = ""
    media_type: str = "text/markdown"
    text: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _digest_is_the_texts(self) -> DocumentBlob:
        if content_hash(self.text) != self.content_hash:
            msg = (
                f"content_hash of document {self.corpus_id}/{self.document_id} "
                "is not the SHA-256 of its text"
            )
            raise ValueError(msg)
        return self


@runtime_checkable
class DocumentFetcher(Protocol):
    """Optional ``RetrievalProvider`` capability: fetch one whole document.

    The gate escrows each document its passages came from (through the
    ledger's evidence escrow) when it decides, and fetches it again when a
    decision is verified live. A provider without it gets no escrow and no
    live verification: its passages verify ``not_verifiable``.
    """

    def fetch_document(
        self, corpus: CorpusRef, document_id: str, version: str | None
    ) -> DocumentBlob:
        """Return one document of ``corpus``, whole.

        Args:
            corpus: The reference, as the binding named it when the
                passage was retrieved.
            document_id: The document.
            version: The version wanted (a corpus may hold several versions
                of one document; the reference provider requires one).

        Returns:
            The document.

        Raises:
            DocumentNotHeldError: The provider holds the corpus but not
                that document version.
            Exception: Anything else that keeps the document from being
                read (the corpus not held, the store unreachable).
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
