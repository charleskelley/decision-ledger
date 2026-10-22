"""Corpus references — what a reasoner's binding to the policy gate names (DR-34).

A corpus is a *reference*, not something the gate manages. A reasoner's
registration names, on its binding to the RAG policy gate, the corpora the
gate retrieves from for it (``PolicyBindingConfig``, the plugin's
``binding_config_model``): for each one, the provider that holds it, the
provider's resource name if it needs one, its authority tier (1 is the
highest), the jurisdictions the binding reads from it, and the weakest
provenance the gate may record for its passages. Two reasoners may bind the
same corpus; the corpus loader loads it once.

A provider (``policy_rag_gate.providers``) answers for a corpus with a
``CorpusState`` — a state marker (the reference provider's manifest hash),
the provenance level it can supply, and its own version — which the gate
records with every query, so a bundle names the state of each corpus it
read without the gate claiming the corpus can be reproduced.

The framework never reads any of this: the binding's ``config`` is opaque
JSON to it, validated by the plugin's ``binding_config_model`` and admitted
by the gate (``PolicyGate.validate_binding``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

#: How much of a retrieved passage's origin a provider can pin down, weakest
#: first in ``PROVENANCE_ORDER``:
#:
#: * ``text_only`` — the passage text alone;
#: * ``uri_and_hash`` — the text plus the URI of the document it came from,
#:   whose content the gate hashes;
#: * ``pinned_version`` — the text plus the exact document version, which the
#:   provider keeps and can return whole (the reference provider).
ProvenanceLevel = Literal["pinned_version", "uri_and_hash", "text_only"]

#: Provenance levels, weakest first.
PROVENANCE_ORDER: tuple[ProvenanceLevel, ...] = (
    "text_only",
    "uri_and_hash",
    "pinned_version",
)

#: The provider a corpus reference names when it names none.
DEFAULT_PROVIDER = "reference"

#: Default passages retrieved from each bound corpus per decision.
DEFAULT_K_PER_CORPUS = 5

#: Most passages a binding may ask of one corpus per decision (every one is
#: rendered into the prompt).
MAX_K_PER_CORPUS = 50

#: A corpus or provider id: letters, digits, ``.``, ``_``, and ``-``,
#: starting with a letter or digit.
_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"


def provenance_at_least(level: ProvenanceLevel, minimum: ProvenanceLevel) -> bool:
    """Whether ``level`` pins a passage down at least as well as ``minimum``.

    Args:
        level: The provenance a provider supplies.
        minimum: The provenance a binding requires.

    Returns:
        ``True`` when ``level`` is ``minimum`` or stronger.
    """
    return PROVENANCE_ORDER.index(level) >= PROVENANCE_ORDER.index(minimum)


class CorpusRef(BaseModel):
    """One corpus a reasoner's binding names, with its authority tier.

    Lax (non-strict) so the binding's JSON validates the way it arrives
    (a list for ``jurisdictions``), except that the tier must be a JSON
    integer. Unknown keys are refused.

    Attributes:
        corpus_id: The corpus, as its provider knows it (the reference
            provider: the ``--corpus-id`` the corpus loader loaded it under).
        provider: The provider that holds it (an entry point in the
            ``policy_rag_gate.providers`` group); ``"reference"`` by default.
        resource: The provider's own name for the corpus when it needs one
            (a cloud corpus resource name). The reference provider takes
            none.
        tier: Authority tier: ``1`` is the highest. The gate renders
            evidence grouped by tier, highest first, and never merges
            scores across corpora.
        jurisdictions: The jurisdictions the binding reads from this corpus.
            Retrieval from it is filtered to them, intersected with a
            submission's own jurisdiction filter when it sets one (a corpus
            whose jurisdictions the submission's filter excludes is not
            queried). Empty: no filter of the binding's own.
        min_provenance: The weakest provenance the gate may record for this
            corpus's passages: the gate refuses the binding when the provider
            cannot supply it, and fails a decision whose passages fall below
            it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    corpus_id: str = Field(pattern=_ID_PATTERN, max_length=128)
    provider: str = Field(default=DEFAULT_PROVIDER, pattern=_ID_PATTERN, max_length=64)
    resource: str | None = Field(default=None, min_length=1)
    tier: int = Field(strict=True, ge=1)
    jurisdictions: frozenset[Annotated[str, Field(min_length=1)]] = frozenset()
    min_provenance: ProvenanceLevel = "text_only"

    @field_serializer("jurisdictions")
    def _sorted(self, jurisdictions: frozenset[str]) -> list[str]:
        """Serialize the set sorted, so every process records it alike."""
        return sorted(jurisdictions)


class PolicyBindingConfig(BaseModel):
    """The policy gate's binding configuration (its ``binding_config_model``).

    What a reasoner's registration says, on its binding to the policy gate,
    about where the gate retrieves its evidence. Validated by the framework
    when a registration is composed or written (DR-33) and admitted by the
    gate (``PolicyGate.validate_binding``); the gate reads it from every
    request (``GateRequest.binding_config``), so a decision retrieves from
    the corpora its registration version names.

    Attributes:
        corpora: The corpora to retrieve from, at least one, each at most
            once (a corpus has one tier in a binding). Order breaks ties
            between corpora of one tier.
        k_per_corpus: Passages retrieved from each corpus per decision (1 to
            ``MAX_K_PER_CORPUS``). Every corpus gets the same quota; scores
            are ranked within a corpus only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    corpora: tuple[CorpusRef, ...] = Field(min_length=1)
    k_per_corpus: int = Field(
        default=DEFAULT_K_PER_CORPUS, strict=True, ge=1, le=MAX_K_PER_CORPUS
    )

    @model_validator(mode="after")
    def _each_corpus_once(self) -> PolicyBindingConfig:
        ids = [corpus.corpus_id for corpus in self.corpora]
        repeated = sorted({corpus_id for corpus_id in ids if ids.count(corpus_id) > 1})
        if repeated:
            msg = (
                f"corpora {repeated} are bound more than once: a corpus has one "
                "tier in a binding"
            )
            raise ValueError(msg)
        return self

    @property
    def tiers(self) -> tuple[int, ...]:
        """The distinct authority tiers bound, highest (lowest number) first."""
        return tuple(sorted({corpus.tier for corpus in self.corpora}))


class CorpusState(BaseModel):
    """What a provider reports about a corpus when asked (``describe``).

    Recorded with every query the gate runs against the corpus, so a bundle
    names the state of each corpus it read. No provider versions its
    corpus, so the state is a marker, not a promise that retrieval can be
    reproduced.

    Attributes:
        corpus_id: The corpus.
        provider: The provider that answered.
        provider_version: The provider's own version label (for the
            reference provider: its retrieval stack and models).
        state_marker: What identifies the corpus's current content, when
            the provider has one (the reference provider: the manifest hash
            of the documents last loaded); ``None`` otherwise.
        provenance_level: The provenance the provider supplies for this
            corpus's passages.
        document_count: Documents in the corpus, when the provider knows.
        updated_at: When the corpus last changed (the reference provider:
            when it was last loaded), when the provider knows.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    corpus_id: str
    provider: str
    provider_version: str
    state_marker: str | None
    provenance_level: ProvenanceLevel
    document_count: int | None = Field(default=None, ge=0)
    updated_at: datetime | None = None
