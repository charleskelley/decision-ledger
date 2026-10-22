"""PolicyGateSnapshot — schema of the policy gate's ``evidence_snapshot`` (v2).

Everything the LLM-backed policy gate consulted to reach a verdict: the
query it sent, one ``CorpusQuery`` per corpus the reasoner's binding names
(the corpus reference, the state its provider reported, the quota, the
filters, the ranking requested and applied, how long it took), the passages
it rendered (``RetrievedContext``, the evidence contract, deduplicated by
content hash), how it laid them out, and the exact prompt it sent (model,
template identity and verbatim snapshot, substitutions, rendered text). The
gate serializes one of these with ``model_dump(mode="json")`` into
``GateOutput.evidence_snapshot``; the framework stores it verbatim and never
reads it (DR-28). Readers that know this gate re-validate it with
``PolicyGateSnapshot.model_validate(...)``.

The bundle is the evidence and the corpus only a source (DR-34): the
snapshot keeps by value what the model saw and by reference how it was
produced. With the template snapshot and the substitutions, an auditor can
verify that ``rendered_prompt`` was built correctly without querying the
prompt registry, a provider, or a corpus.

Version 2 (DR-34) replaced the single-corpus fields of version 1 — the
reasoner's ``retrieval_results`` (``RetrievedSnippet``), one
``retrieval_path``, and the submitted ``corpus_version`` label — with
per-corpus queries, the evidence contract, and the layout; a bundle decided
before it keeps the version-1 shape, which replay never reads.

Every field is a typed attribute; the opacity is at the framework boundary,
not inside the gate.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from policy_rag_gate.corpus import CorpusRef, CorpusState
from policy_rag_gate.models.context import RetrievedContext
from policy_rag_gate.models.prompt import (
    EvidenceLayout,
    PromptSnapshot,
)


class RetrievalFilters(BaseModel):
    """The filters one corpus was queried with.

    Attributes:
        jurisdictions: The jurisdiction filter (the submission's, narrowed
            by the binding's jurisdictions for the corpus), or ``None`` for
            none.
        risk_tier: The risk-tier filter, or ``None`` for none.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    jurisdictions: tuple[str, ...] | None = None
    risk_tier: str | None = None


class CorpusQuery(BaseModel):
    """One corpus the gate asked for evidence during one invocation.

    Attributes:
        corpus_ref: The reference as the reasoner's binding names it.
        state: The state the provider reported for the corpus when it was
            queried; ``None`` when it was not queried (``skipped``).
        k: Passages asked for (the binding's ``k_per_corpus``).
        filters: The filters the provider was given.
        ranking_requested: The ranking the provider attempted; ``None``
            when the corpus was not queried.
        ranking_applied: The ranking that produced the passages (the
            reference provider: ``"reranked"``, or ``"rrf_only"`` when
            reranking was bypassed), or ``"skipped"``.
        duration_ms: Time the query took, in milliseconds.
        returned: Passages the provider returned.
        merged: Of those, passages not rendered because a passage with the
            same text from a higher-precedence corpus (higher tier, or
            earlier in the binding) or a higher rank was rendered already.
        skipped: Why the corpus was not queried (its jurisdictions and the
            submission's filter do not meet), or ``None``.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    corpus_ref: CorpusRef
    state: CorpusState | None
    k: int = Field(ge=1)
    filters: RetrievalFilters
    ranking_requested: str | None
    ranking_applied: str
    duration_ms: float = Field(ge=0.0)
    returned: int = Field(ge=0)
    merged: int = Field(ge=0)
    skipped: str | None = None


class PolicyGateSnapshot(BaseModel):
    """What the policy gate consulted for one invocation.

    Attributes:
        snapshot_version: ``2`` (DR-34).
        retrieval_query: The reasoner-rendered query string the gate sent
            to each provider (from ``gate_config["retrieval_query"]``).
            Empty when the gate failed before retrieving.
        corpus_queries: One per corpus the binding names, in binding order.
            Empty when the gate failed before retrieving.
        contexts: The passages rendered into the prompt, in rendered order:
            by tier, highest first, then binding order, then rank, each
            distinct text once.
        evidence_layout: How the passages were rendered
            (``"numbered_list"`` or ``"tiered_blocks"``, the template's).
        dedup_merged: Passages retrieved but not rendered because their
            text was rendered already (the sum of ``CorpusQuery.merged``).
        index_version: Version label of the retrieval index.
        model_version: LLM model identifier used for this invocation.
        prompt_template_id: Template identifier resolved from the registry
            (the audit key for the active prompt version).
        prompt_template_version: Semantic version of that template.
        rendered_prompt: Full prompt as sent to the LLM. Empty when
            rendering failed or the gate stopped before rendering.
        prompt_snapshot: Verbatim template snapshot at invocation time.
        template_vars: Domain substitutions used for this invocation.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    snapshot_version: Literal[2] = 2
    retrieval_query: str
    corpus_queries: list[CorpusQuery]
    contexts: list[RetrievedContext]
    evidence_layout: EvidenceLayout
    dedup_merged: int = Field(ge=0)
    index_version: str
    model_version: str
    prompt_template_id: str
    prompt_template_version: str
    rendered_prompt: str
    prompt_snapshot: PromptSnapshot
    template_vars: dict[str, str]
