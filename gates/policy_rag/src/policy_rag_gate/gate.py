"""LLM-backed policy gate — retrieval through providers, prompt rendering, LLM verdict.

``PolicyGate`` is the reference ``Gate`` (DR-28). Given a ``GateRequest`` it
retrieves evidence from every corpus the submitting reasoner's binding names
(``GateRequest.binding_config``, a ``PolicyBindingConfig``; DR-34), each
through the retrieval provider that holds it — the same quota from every
corpus, ranked within a corpus only, never fused across corpora — drops a
passage whose text a higher-precedence corpus already supplied, renders the
passages into the reasoner's prompt template the way the template's
``evidence_layout`` says (one numbered list, or blocks by authority tier,
highest first), calls the LLM through the SDK-agnostic ``LLMClient``
(DR-23) with the template's own ``system_text`` as the system message, and
returns the universal ``GateOutput``:

- ``verdict`` — the universal ``GateVerdict`` part of the model's verdict
  (``None`` on any failure). A template with tiered evidence asks for a
  ``TieredPolicyGateVerdict``; when the model reports passages on point
  that disagree, or evidence that does not cover the case, the gate holds
  the decision for review: the verdict it returns is floored at HOLD (a
  BLOCK stays a BLOCK) and escalates to a human, and the model's own is
  kept in ``extra``;
- ``evidence`` — one ``EvidenceRef`` per rendered passage, carrying its
  jurisdiction (enforcement's jurisdiction-conflict tier sees exactly the
  rendered set), the SHA-256 of its text, and the digest of the document
  the gate escrowed for it (``artifact_hash``);
- ``evidence_snapshot`` — a ``PolicyGateSnapshot`` (v2): one ``CorpusQuery``
  per bound corpus (reference, provider state, quota, filters, ranking
  requested and applied, duration), the rendered passages under the
  evidence contract (``RetrievedContext``), the layout, and the exact
  prompt (model, template identity and snapshot, substitutions, text);
- ``extra`` — a ``PolicyGateExtra``: rationale, citations, the tiered
  report, token cost, retrieval latency, and the failing stage when there
  is no verdict.

The gate carries no domain wording: everything the model reads comes from
the reasoner's template file and the retrieved passages, whose layout adds
only labels (corpus, tier, document, version, section). What the gate needs
from the reasoner per decision comes from ``gate_config``, typed by
``PolicyGateConfig`` (the gate's ``config_model``, validated by the
framework at intake): ``template_id``, ``template_vars``,
``retrieval_query``, ``jurisdictions``, and ``risk_tier``. What it needs per
reasoner comes from the binding: the corpora and the quota.

Who may send what is the gate's call, not the registration's (DR-28): when
built with per-reasoner ``ReasonerProfile`` objects, ``authorize_config``
— which the framework calls at intake — accepts only templates the
submitting reasoner ships and jurisdictions its corpus covers.

Which reasoners it serves, and from which corpora, is the gate's call too
(DR-33, DR-34): the framework asks ``validate_binding`` about every
registration that binds the gate, at startup and when a registration is
written over the API. The gate admits a binding whose corpora every named
provider can describe at the provenance the binding asks for. A reasoner
the deployment composes as a plugin is served from its plugin's assets; any
other is admitted from its directory under the asset root
(``POLICY_RAG_GATE_ASSETS_ROOT``, ``policy_rag_gate.assets``), and
``serves`` then answers yes.

Evidence escrow and verify (DR-34 part 2): right after retrieval the gate
fetches each document its passages came from, once (through the provider's
optional ``DocumentFetcher``), hands its text to the ledger's evidence
escrow (the ``EvidenceEscrow`` its plugin took from the framework's store),
and cites it by the digest the escrow returned. ``verify`` later says, for
a stored output, how each reference stands: against the escrowed document
(the passage's ``span`` in it hashes to the passage's digest), or live
against the document as its provider holds it now. Verify never calls the
LLM or retrieves again, and replay never calls verify.

Strict structured output is enforced at the SDK layer — the ``LLMClient``
returns a Pydantic-validated verdict directly. Any failure to reach a
verdict (no retrieval query, template render error, LLM error or
SDK-boundary schema refusal) produces ``verdict=None``, which enforcement
Tier 1 routes to HOLD deterministically. An unknown ``template_id``, a
binding configuration that does not validate, a provider error, or a
provider answer that breaks the evidence contract propagates to the caller
(the framework fails the gate closed).

Usage::

    gate = PolicyGate(client=client, prompt_registry=registry, providers=providers)
    output = await gate.evaluate(request)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import structlog
from pydantic import ValidationError

from app.monitoring import duration_ms
from core.actions import DecisionAction
from core.exceptions import GateConfigError
from core.gate import (
    EvidenceRef,
    EvidenceStatus,
    GateOutput,
    GateVerdict,
    severity,
)
from policy_rag_gate.assets import load_root_assets
from policy_rag_gate.config import PolicyGateConfig
from policy_rag_gate.corpus import (
    CorpusState,
    PolicyBindingConfig,
    provenance_at_least,
)
from policy_rag_gate.models import (
    CorpusQuery,
    PolicyGateExtra,
    PolicyGateSnapshot,
    PolicyGateVerdict,
    PromptSnapshot,
    RetrievalFilters,
    RetrievedContext,
    TieredPolicyGateVerdict,
    TokenCost,
    content_hash,
)
from policy_rag_gate.providers import (
    DocumentBlob,
    DocumentFetcher,
    DocumentNotHeldError,
    ProviderContractError,
    ProviderResult,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from core.gate_request import GateRequest
    from core.llm import LLMClient
    from core.llm.client import TokenUsage
    from policy_rag_gate.config import ReasonerProfile
    from policy_rag_gate.corpus import CorpusRef
    from policy_rag_gate.models import (
        EvidenceLayout,
        PolicyGateFailure,
        PromptRegistry,
        ProtocolHold,
    )
    from policy_rag_gate.providers import ProviderRegistry

log = structlog.get_logger(__name__)

#: The policy gate's identifier — what ``GateContext.gate_id`` names.
POLICY_GATE_ID = "policy"

#: What ``{policy_snippets}`` holds when no passage was retrieved.
NO_EVIDENCE = "(no policy evidence retrieved)"

#: The ranking recorded for a corpus the gate did not query.
SKIPPED = "skipped"

# The gate's escalation reasons when it holds a tiered verdict: constant
# text, nothing the model wrote in it (the corpus it named governing is kept
# in ``extra``). Worded without "conflict" or "jurisdiction": enforcement
# reads either word in an escalation as a jurisdiction conflict (Tier 5),
# and this is not one; the gate's held verdict is accepted as its output.
_DISAGREE_REASON = (
    "Retrieved policy evidence on point disagrees; held for human review."
)
_GAP_REASON = (
    "Retrieved policy evidence does not cover this case; held for human review."
)


class _Closeable(Protocol):
    def close(self) -> None: ...


class EvidenceEscrow(Protocol):
    """The ledger's evidence escrow, as the gate uses it (DR-34 part 2).

    The framework's store satisfies it (``framework.store.artifacts``): the
    gate's plugin takes it when the gate is built. ``enabled`` is the store's
    own state (the deployment turned escrow off when it is ``False``); the
    gate reads it only to skip fetching documents nothing would keep.
    """

    @property
    def enabled(self) -> bool:
        """Whether ``put`` keeps anything."""
        ...

    def put(
        self, content: bytes, *, media_type: str, decision_id: str, gate_id: str
    ) -> str | None:
        """Escrow ``content``; its digest, or ``None`` when escrow is off."""
        ...

    def get(self, artifact_hash: str) -> bytes | None:
        """The escrowed bytes with that digest, or ``None``."""
        ...


# ---------------------------------------------------------------------------
# Private helpers (pure — no I/O; testable without infrastructure)
# ---------------------------------------------------------------------------


def _heading(context: RetrievedContext, *, with_version: bool) -> str:
    """Title, version (optionally), and jurisdiction of a passage."""
    heading = context.title
    if with_version and context.version is not None:
        heading += f" (v{context.version})"
    if context.jurisdiction is not None:
        heading += f" — {context.jurisdiction}"
    return heading


def render_numbered_list(contexts: Sequence[RetrievedContext]) -> str:
    """One numbered list of passages (the ``numbered_list`` layout).

    Byte for byte the rendering every template written before DR-34 was
    filled with: ``[n] <title> (v<version>) — <jurisdiction>``, the section,
    then the text. A field the provider does not supply is left out.
    """
    if not contexts:
        return NO_EVIDENCE
    parts = []
    for number, context in enumerate(contexts, 1):
        section = (
            f"Section: {context.section_path}\n"
            if context.section_path is not None
            else ""
        )
        parts.append(
            f"[{number}] {_heading(context, with_version=True)}\n"
            f"{section}{context.text}"
        )
    return "\n\n".join(parts)


def render_tiered_blocks(contexts: Sequence[RetrievedContext]) -> str:
    """Passages in blocks by authority tier, highest first (``tiered_blocks``).

    Each block opens with its tier and the corpora in it; each passage is
    numbered across blocks (so a citation can name it) and labeled with its
    corpus, tier, document, version, and section. The labels are the only
    text the gate adds.
    """
    if not contexts:
        return NO_EVIDENCE
    blocks = []
    number = 0
    for tier in sorted({context.tier for context in contexts}):
        in_tier = [context for context in contexts if context.tier == tier]
        corpora = ", ".join(dict.fromkeys(context.corpus_id for context in in_tier))
        parts = [f"--- Tier {tier} (corpora: {corpora}) ---"]
        for context in in_tier:
            number += 1
            label = (
                f"Corpus: {context.corpus_id} | Tier: {context.tier} | "
                f"Document: {context.document_id}"
            )
            if context.version is not None:
                label += f" | Version: {context.version}"
            section = (
                f"\nSection: {context.section_path}"
                if context.section_path is not None
                else ""
            )
            parts.append(
                f"[{number}] {_heading(context, with_version=False)}\n"
                f"{label}{section}\n{context.text}"
            )
        blocks.append("\n\n".join(parts))
    return "\n\n".join(blocks)


def render_evidence(
    contexts: Sequence[RetrievedContext], layout: EvidenceLayout
) -> str:
    """Render ``contexts`` for ``{policy_snippets}`` in ``layout``."""
    if layout == "tiered_blocks":
        return render_tiered_blocks(contexts)
    return render_numbered_list(contexts)


def _render_prompt(
    template_str: str,
    template_vars: dict[str, str],
    evidence_text: str,
) -> str:
    """Render a prompt template with domain variables and the rendered evidence.

    Injects the evidence first (with brace-escaping to prevent
    ``str.format()`` from misinterpreting any ``{ }`` characters in passage
    text as placeholders), then substitutes all domain ``template_vars``.

    Raises:
        KeyError: If any ``{placeholder}`` in ``template_str`` is absent
            from ``template_vars`` — indicates a mismatch in the domain
            assembler.
    """
    escaped = evidence_text.replace("{", "{{").replace("}", "}}")
    interim = template_str.replace("{policy_snippets}", escaped)
    return interim.format(**template_vars)


def _build_token_cost(usage: TokenUsage, model: str) -> TokenCost:
    """Translate a neutral ``TokenUsage`` into the audit-side ``TokenCost``."""
    return TokenCost(
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        cost_usd=round(usage.cost_usd if usage.cost_usd is not None else 0.0, 6),
        model=model,
    )


def _evidence(contexts: Sequence[RetrievedContext]) -> tuple[EvidenceRef, ...]:
    """One ``EvidenceRef`` per rendered passage, with its jurisdiction and digest.

    Enforcement's jurisdiction-conflict tier reads the union of these
    jurisdictions, so it sees exactly the rendered set.
    """
    return tuple(
        EvidenceRef(
            source_id=context.document_id,
            locator=context.section_path,
            jurisdictions=frozenset({context.jurisdiction})
            if context.jurisdiction
            else frozenset(),
            content_hash=context.content_hash,
            artifact_hash=context.artifact_hash,
        )
        for context in contexts
    )


def corpus_filter(
    corpus: CorpusRef, submitted: Sequence[str]
) -> tuple[list[str] | None, str | None]:
    """The jurisdiction filter for one corpus, or why it is not queried.

    The submission's filter, narrowed to the jurisdictions the binding reads
    from the corpus when it names any.

    Args:
        corpus: The bound corpus.
        submitted: The submission's jurisdiction filter (empty: none).

    Returns:
        ``(filter, None)`` with ``None`` for no filter, or ``(None, reason)``
        when the binding's jurisdictions and the submission's filter do not
        meet (the corpus is not queried).
    """
    if not corpus.jurisdictions:
        return (list(submitted) or None), None
    if not submitted:
        return sorted(corpus.jurisdictions), None
    both = [
        jurisdiction
        for jurisdiction in submitted
        if jurisdiction in corpus.jurisdictions
    ]
    if both:
        return both, None
    reason = (
        f"the binding reads {sorted(corpus.jurisdictions)} from this corpus and the "
        f"submission filters to {list(submitted)}"
    )
    return None, reason


def check_contract(corpus: CorpusRef, result: object, *, k: int) -> ProviderResult:
    """Hold a provider's answer to the evidence contract for ``corpus``.

    Args:
        corpus: The reference the provider was asked about.
        result: What the provider returned.
        k: The quota it was asked for.

    Returns:
        The result, a ``ProviderResult`` (whose passages validated as
        ``RetrievedContext`` when the provider built it).

    Raises:
        ProviderContractError: It is not a ``ProviderResult``, or answers for
            another corpus, provider, or tier, returns more than ``k``
            passages or ranks them out of order, or returns a passage below
            the binding's ``min_provenance``.
    """
    if not isinstance(result, ProviderResult):
        msg = (
            f"provider {corpus.provider!r} returned {type(result).__name__}, not a "
            "ProviderResult: the evidence contract is its return model"
        )
        raise ProviderContractError(msg)
    typed = result
    where = f"provider {corpus.provider!r} answering for corpus {corpus.corpus_id!r}"
    if not isinstance(typed.state, CorpusState):
        msg = f"{where} reported a {type(typed.state).__name__}, not a CorpusState"
        raise ProviderContractError(msg)
    if (typed.state.corpus_id, typed.state.provider) != (
        corpus.corpus_id,
        corpus.provider,
    ):
        msg = (
            f"{where} reported the state of "
            f"{typed.state.provider}/{typed.state.corpus_id}"
        )
        raise ProviderContractError(msg)
    if len(typed.contexts) > k:
        msg = f"{where} returned {len(typed.contexts)} passages for a quota of {k}"
        raise ProviderContractError(msg)
    for expected_rank, context in enumerate(typed.contexts, 1):
        # Re-checked here: a result built without validation
        # (``model_construct``) would otherwise record a digest the model
        # never saw.
        if not isinstance(context, RetrievedContext):
            msg = f"{where} returned a {type(context).__name__}, not a RetrievedContext"
            raise ProviderContractError(msg)
        if content_hash(context.text) != context.content_hash:
            msg = (
                f"{where} returned passage {expected_rank} with a content_hash "
                "that is not the SHA-256 of its text"
            )
            raise ProviderContractError(msg)
        if (context.corpus_id, context.provider, context.tier) != (
            corpus.corpus_id,
            corpus.provider,
            corpus.tier,
        ):
            msg = (
                f"{where} returned a passage labeled {context.provider}/"
                f"{context.corpus_id} at tier {context.tier} (bound at tier "
                f"{corpus.tier})"
            )
            raise ProviderContractError(msg)
        if context.rank != expected_rank:
            msg = f"{where} ranked its passages {[c.rank for c in typed.contexts]}"
            raise ProviderContractError(msg)
        if not provenance_at_least(context.provenance_level, corpus.min_provenance):
            msg = (
                f"{where} returned a passage with {context.provenance_level!r} "
                f"provenance, below the binding's min_provenance "
                f"{corpus.min_provenance!r}"
            )
            raise ProviderContractError(msg)
    return typed


def _held(verdict: TieredPolicyGateVerdict) -> tuple[GateVerdict, ProtocolHold | None]:
    """The universal verdict, held for review when the tiered report says so.

    A hold floors the verdict at HOLD and never relaxes it: of the actions
    the model permitted, those at least as restrictive as HOLD are kept (a
    BLOCK stays a BLOCK, since enforcement takes the most conservative
    permitted action), and HOLD stands in for the milder ones.
    """
    universal = verdict.universal()
    if verdict.conflict_detected:
        hold: ProtocolHold = "conflict_detected"
        reason = _DISAGREE_REASON
    elif not verdict.evidence_sufficient:
        hold = "evidence_insufficient"
        reason = _GAP_REASON
    else:
        return universal, None
    floor = severity(DecisionAction.HOLD)
    kept = [a for a in universal.permitted_actions if severity(a) >= floor]
    held = GateVerdict(
        gate_id=universal.gate_id,
        permitted_actions=kept or [DecisionAction.HOLD],
        required_controls=list(universal.required_controls),
        confidence=universal.confidence,
        escalate_to_human=True,
        escalation_reason=reason,
    )
    return held, hold


def _policy_gate_output(
    *,
    verdict: GateVerdict | None,
    contexts: Sequence[RetrievedContext],
    snapshot: PolicyGateSnapshot,
    extra: PolicyGateExtra,
) -> GateOutput:
    """Assemble the universal ``GateOutput`` the policy gate returns."""
    return GateOutput(
        gate_id=POLICY_GATE_ID,
        deterministic=False,
        verdict=verdict,
        evidence=_evidence(contexts),
        evidence_snapshot=snapshot.model_dump(mode="json"),
        extra=extra.model_dump(mode="json"),
    )


@dataclass(frozen=True)
class Evidence:
    """What retrieval over one binding produced for one query.

    Attributes:
        queries: One record per bound corpus, in binding order.
        contexts: The passages to render, in precedence order (tier, highest
            first; then binding order; then rank), each distinct text once.
        dedup_merged: Passages retrieved but merged into an earlier passage
            with the same text.
        results: Each corpus's full answer (before merging), by corpus id;
            ``None`` for a corpus that was not queried.
    """

    queries: list[CorpusQuery]
    contexts: list[RetrievedContext]
    dedup_merged: int
    results: dict[str, ProviderResult | None]


def retrieve_evidence(
    providers: ProviderRegistry,
    binding: PolicyBindingConfig,
    query: str,
    *,
    jurisdictions: Sequence[str] = (),
    risk_tier: str | None = None,
) -> Evidence:
    """Query every corpus a binding names, as the gate does for a decision.

    Corpora are queried in binding order, each for the binding's
    ``k_per_corpus`` passages with the submission's filters (the
    jurisdiction filter narrowed by the binding's jurisdictions for the
    corpus; a corpus they exclude is not queried), and every answer is held
    to the evidence contract. Scores are never compared across corpora: the
    passages are kept in precedence order — tier (highest first), then
    binding order, then rank — with each distinct text once (a passage
    whose text a higher-precedence passage carries is merged into it).

    Args:
        providers: The providers bindings may name.
        binding: The reasoner's binding.
        query: The reasoner-rendered retrieval query.
        jurisdictions: The submission's jurisdiction filter (empty: none).
        risk_tier: The submission's risk-tier filter.

    Returns:
        The queries, the passages to render, and the merges.

    Raises:
        LookupError: A provider is not installed.
        ProviderContractError: A provider's answer breaks the evidence
            contract.
        Exception: Whatever a provider raises.
    """
    answered: list[tuple[CorpusRef, ProviderResult | None, CorpusQuery]] = []
    for corpus in binding.corpora:
        corpus_jurisdictions, skipped = corpus_filter(corpus, jurisdictions)
        filters = RetrievalFilters(
            jurisdictions=tuple(corpus_jurisdictions)
            if corpus_jurisdictions is not None
            else None,
            risk_tier=risk_tier,
        )
        t0 = time.perf_counter()
        result: ProviderResult | None = None
        if skipped is None:
            provider = providers.get(corpus.provider)
            result = check_contract(
                corpus,
                provider.retrieve(
                    query,
                    corpus,
                    k=binding.k_per_corpus,
                    jurisdictions=corpus_jurisdictions,
                    risk_tier=risk_tier,
                ),
                k=binding.k_per_corpus,
            )
        record = CorpusQuery(
            corpus_ref=corpus,
            state=result.state if result is not None else None,
            k=binding.k_per_corpus,
            filters=filters,
            ranking_requested=result.ranking_requested if result is not None else None,
            ranking_applied=result.ranking_applied if result is not None else SKIPPED,
            duration_ms=duration_ms(t0),
            returned=len(result.contexts) if result is not None else 0,
            merged=0,
            skipped=skipped,
        )
        answered.append((corpus, result, record))

    # A passage is merged only into one with the same text under the same
    # jurisdiction: the same words in another jurisdiction's document are
    # other evidence (enforcement's jurisdiction-conflict tier reads every
    # rendered passage's jurisdiction).
    seen: set[tuple[str, str | None]] = set()
    contexts: list[RetrievedContext] = []
    merged: dict[str, int] = {}
    for corpus, result, _ in sorted(answered, key=lambda item: item[0].tier):
        for context in result.contexts if result is not None else ():
            key = (context.content_hash, context.jurisdiction)
            if key in seen:
                merged[corpus.corpus_id] = merged.get(corpus.corpus_id, 0) + 1
                continue
            seen.add(key)
            contexts.append(context)
    return Evidence(
        queries=[
            record.model_copy(update={"merged": merged.get(corpus.corpus_id, 0)})
            for corpus, _, record in answered
        ],
        contexts=contexts,
        dedup_merged=sum(merged.values()),
        results={corpus.corpus_id: result for corpus, result, _ in answered},
    )


# ---------------------------------------------------------------------------
# Evidence escrow and verify (DR-34 part 2)
# ---------------------------------------------------------------------------

#: Which document a passage came from: corpus, document, version.
_DocumentKey = tuple[str, str, str | None]

#: The snapshot version whose references ``verify`` can match to passages.
SNAPSHOT_VERSION: int = PolicyGateSnapshot.model_fields["snapshot_version"].default


def _check_document(document: object, key: _DocumentKey) -> DocumentBlob:
    """Hold a fetched document to what was asked for, or refuse it."""
    corpus_id, document_id, version = key
    if (
        not isinstance(document, DocumentBlob)
        or (document.corpus_id, document.document_id) != (corpus_id, document_id)
        or (version is not None and document.version != version)
        or content_hash(document.text) != document.content_hash
    ):
        msg = (
            f"asked for {corpus_id}/{document_id} version {version!r}, the provider "
            f"returned {type(document).__name__} that is not that document whole"
        )
        raise ProviderContractError(msg)
    return document


class EvidenceEscrowError(RuntimeError):
    """The document fetched to escrow no longer holds a passage cited to it.

    The document changed between retrieval and escrow (a reload of the same
    version racing the decision): the escrow could never prove what the
    decision relied on, so the gate fails closed and the decision is held.
    """


def escrow_documents(
    providers: ProviderRegistry,
    queries: Sequence[CorpusQuery],
    contexts: Sequence[RetrievedContext],
    escrow: EvidenceEscrow,
    *,
    decision_id: str,
) -> list[RetrievedContext]:
    """Escrow the document each passage came from, and cite it on the passage.

    Each ``(corpus, document, version)`` is fetched once, from the provider
    of the corpus query the passage was retrieved by, and its text is handed
    to the escrow as UTF-8. Every passage from that document then carries
    the digest the escrow returned as its ``artifact_hash`` (none when the
    deployment runs with escrow off, in which case nothing is fetched at
    all). A provider without ``DocumentFetcher`` escrows nothing for its
    passages. A fetched document must still hold every passage cited to it.
    Blocking: run it in a thread.

    Args:
        providers: The providers bindings may name.
        queries: The decision's corpus queries (each passage's corpus has one).
        contexts: The passages to render.
        escrow: The ledger's evidence escrow.
        decision_id: The decision being decided.

    Returns:
        The passages, in order, citing their escrowed documents.

    Raises:
        ProviderContractError: A provider returned another document than asked.
        EvidenceEscrowError: A fetched document no longer holds a passage
            retrieved from it.
        Exception: Whatever a provider or the escrow raises (the gate fails
            closed: a decision whose evidence cannot be escrowed is held).
    """
    if not escrow.enabled:
        return list(contexts)
    corpora = {query.corpus_ref.corpus_id: query.corpus_ref for query in queries}
    cited: dict[_DocumentKey, list[RetrievedContext]] = {}
    for context in contexts:
        key = (context.corpus_id, context.document_id, context.version)
        cited.setdefault(key, []).append(context)
    digests: dict[_DocumentKey, str | None] = {}
    for key, passages in cited.items():
        corpus = corpora[key[0]]
        provider = providers.get(corpus.provider)
        if not isinstance(provider, DocumentFetcher):
            digests[key] = None
            continue
        document = _check_document(provider.fetch_document(corpus, key[1], key[2]), key)
        stale = [p.rank for p in passages if not passage_holds(document.text, p)]
        if stale:
            msg = (
                f"{key[0]}/{key[1]} version {key[2]!r}, fetched to escrow, no longer "
                f"holds the passage(s) ranked {stale} retrieved from it: it changed "
                "while the decision was being made"
            )
            raise EvidenceEscrowError(msg)
        digests[key] = escrow.put(
            document.text.encode("utf-8"),
            media_type=document.media_type,
            decision_id=decision_id,
            gate_id=POLICY_GATE_ID,
        )
    escrowed: list[RetrievedContext] = []
    for context in contexts:
        digest = digests[(context.corpus_id, context.document_id, context.version)]
        escrowed.append(
            context
            if digest is None
            else context.model_copy(update={"artifact_hash": digest})
        )
    return escrowed


def stored_snapshot(output: GateOutput) -> PolicyGateSnapshot | None:
    """The output's evidence snapshot, when it is a version-2 policy snapshot.

    Args:
        output: A stored policy-gate output.

    Returns:
        The snapshot, or ``None`` for an older snapshot or one that does not
        validate (its references cannot be matched to their passages).
    """
    raw = output.evidence_snapshot
    if not isinstance(raw, dict) or raw.get("snapshot_version") != SNAPSHOT_VERSION:
        return None
    try:
        return PolicyGateSnapshot.model_validate_json(json.dumps(raw))
    except ValidationError:
        return None


def passage_holds(text: str, context: RetrievedContext) -> bool:
    """Whether a document's text still holds a passage as the decision cited it.

    With a ``span``, the text at those offsets must hash to the passage's
    digest. Without one (the loader merged a short section into the next and
    dropped the heading between them, so the passage is not contiguous in
    the document), each of its paragraphs must occur in the document, in
    order.

    Args:
        text: A document's whole text (escrowed, or as its provider holds it).
        context: The passage, as the snapshot stores it.

    Returns:
        ``True`` when the document holds the passage.
    """
    if context.span is not None:
        start, end = context.span
        return (
            end <= len(text) and content_hash(text[start:end]) == context.content_hash
        )
    cursor = 0
    for paragraph in context.text.split("\n\n"):
        if not paragraph.strip():
            continue
        found = text.find(paragraph, cursor)
        if found < 0:
            return False
        cursor = found + len(paragraph)
    return True


def _escrowed_text(content: bytes | None, digest: str) -> str | None:
    """An escrowed document's text, or ``None`` when absent or not what was kept."""
    if content is None or hashlib.sha256(content).hexdigest() != digest:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


# ---------------------------------------------------------------------------
# PolicyGate
# ---------------------------------------------------------------------------


class PolicyGate:
    """LLM-backed policy gate: per-corpus retrieval plus a structured LLM verdict.

    Satisfies ``app.gates.Gate`` and its optional ``ConfigAuthorizer``,
    ``BindingValidator``, ``ReasonerScope``, and ``EvidenceVerifier``
    capabilities. Owns its providers, prompt registry, LLM client, and
    per-reasoner profiles; ``close()`` releases the providers it built and
    the handles it was given.

    Args:
        client: SDK-agnostic ``LLMClient`` adapter. Decouples gate
            behavior from the underlying LLM provider — see DR-23.
        prompt_registry: Registry for resolving versioned prompt
            templates (every composed reasoner's ``prompts_dir``).
        providers: The retrieval providers bindings may name
            (``policy_rag_gate.providers``), built on first use.
        index_version: Version label of the retrieval index, recorded in
            every snapshot.
        model_version_label: Free-form label recorded as the snapshot's
            ``model_version``. Defaults to ``client.model`` when the client
            exposes a ``model`` property; otherwise an opaque
            ``"<llm-client>"`` placeholder.
        profiles: What each reasoner may submit, keyed by ``reasoner_id``:
            the composed plugins' (the plugin derives them from each
            reasoner's prompts and corpus), to which ``validate_binding``
            adds the reasoners it admits from the asset root. ``None``
            authorizes every submission and serves everyone — a gate
            assembled by hand for a test or a fault-injection run.
        resources: Infrastructure handles the gate owns besides its
            providers and closes on ``close()``.
        assets_root: The asset root a reasoner registered without a plugin
            ships its prompts and corpus under
            (``POLICY_RAG_GATE_ASSETS_ROOT``), or ``None`` for none.
        escrow: The ledger's evidence escrow (the framework store's
            ``artifacts``), or ``None`` for a gate built without one: it then
            escrows nothing, and ``verify`` checks references live only.
    """

    def __init__(
        self,
        *,
        client: LLMClient,
        prompt_registry: PromptRegistry,
        providers: ProviderRegistry,
        index_version: str = "unknown",
        model_version_label: str | None = None,
        profiles: Mapping[str, ReasonerProfile] | None = None,
        resources: Sequence[_Closeable] = (),
        assets_root: Path | None = None,
        escrow: EvidenceEscrow | None = None,
    ) -> None:
        """Initialize the policy gate with its client, registry, and providers."""
        self._client = client
        self._registry = prompt_registry
        self._providers = providers
        self._index_version = index_version
        self._model_version_label = (
            model_version_label
            if model_version_label is not None
            else getattr(client, "model", "<llm-client>")
        )
        self._profiles = dict(profiles) if profiles is not None else None
        self._resources = tuple(resources)
        self._assets_root = assets_root
        self._escrow = escrow
        self._admission = threading.Lock()

    @property
    def client(self) -> LLMClient:
        """The LLM client the gate calls."""
        return self._client

    @property
    def prompt_registry(self) -> PromptRegistry:
        """The prompt registry the gate renders from."""
        return self._registry

    @property
    def escrow(self) -> EvidenceEscrow | None:
        """The ledger's evidence escrow the gate writes to, if it was given one."""
        return self._escrow

    @property
    def providers(self) -> ProviderRegistry:
        """The retrieval providers the gate queries."""
        return self._providers

    @property
    def profiles(self) -> Mapping[str, ReasonerProfile] | None:
        """Per-reasoner profiles the gate authorizes against (``None``: any).

        A snapshot: admitting a reasoner swaps in a new mapping, so a caller
        that keeps this one keeps the profiles as they were.
        """
        return self._profiles

    @property
    def assets_root(self) -> Path | None:
        """The asset root reasoners without a plugin are admitted from."""
        return self._assets_root

    async def verify(
        self, output: GateOutput, *, live: bool
    ) -> tuple[EvidenceStatus, ...]:
        """Say how each evidence reference of an output this gate returned stands.

        Each reference is matched to the passage it was built from (the
        snapshot's ``contexts``, in evidence order; a version-1 snapshot, a
        missing passage, or a digest that is not the passage's leaves it
        ``not_verifiable``). Then, without ``live``, the passage is checked
        against the document escrowed for it: ``intact`` when the escrowed
        text holds it (``passage_holds``), ``source_changed`` when it does
        not, ``not_verifiable`` when nothing was escrowed or the escrow no
        longer holds it. With ``live``, against the document as its provider
        holds it now (the corpus reference comes from the snapshot's corpus
        queries): ``intact`` or ``source_changed``; ``source_changed`` too
        when the provider holds the corpus but no longer that version;
        ``source_unavailable`` when the provider is not installed, cannot be
        built, or cannot read the document (the corpus not loaded, the store
        unreachable); ``not_verifiable`` for a provider without
        ``DocumentFetcher``. Each document is read once per call. Neither
        mode calls the LLM or retrieves.

        Args:
            output: An output this gate returned, as the bundle stores it.
            live: Whether to read each document as its provider holds it now.

        Returns:
            One status per reference in ``output.evidence``, in order.
        """
        return await asyncio.to_thread(self._verify, output, live=live)

    def _verify(self, output: GateOutput, *, live: bool) -> tuple[EvidenceStatus, ...]:
        refs = output.evidence
        snapshot = stored_snapshot(output)
        if snapshot is None:
            return ("not_verifiable",) * len(refs)
        corpora = {
            q.corpus_ref.corpus_id: q.corpus_ref for q in snapshot.corpus_queries
        }
        documents: dict[_DocumentKey, DocumentBlob | EvidenceStatus] = {}
        escrowed: dict[str, str | None] = {}
        statuses: list[EvidenceStatus] = []
        for index, ref in enumerate(refs):
            context = (
                snapshot.contexts[index] if index < len(snapshot.contexts) else None
            )
            if context is None or ref.content_hash != context.content_hash:
                statuses.append("not_verifiable")
            elif live:
                statuses.append(self._live_status(context, corpora, documents))
            else:
                statuses.append(self._stored_status(ref, context, escrowed))
        return tuple(statuses)

    def _stored_status(
        self,
        ref: EvidenceRef,
        context: RetrievedContext,
        escrowed: dict[str, str | None],
    ) -> EvidenceStatus:
        """The passage checked against the document escrowed for it."""
        digest = ref.artifact_hash
        if digest is None or self._escrow is None:
            return "not_verifiable"
        if digest not in escrowed:
            escrowed[digest] = _escrowed_text(self._escrow.get(digest), digest)
        text = escrowed[digest]
        if text is None:
            return "not_verifiable"
        return "intact" if passage_holds(text, context) else "source_changed"

    def _live_status(
        self,
        context: RetrievedContext,
        corpora: Mapping[str, CorpusRef],
        documents: dict[_DocumentKey, DocumentBlob | EvidenceStatus],
    ) -> EvidenceStatus:
        """The passage checked against its document as its provider holds it now."""
        key = (context.corpus_id, context.document_id, context.version)
        if key not in documents:
            documents[key] = self._fetch_now(key, corpora.get(context.corpus_id))
        found = documents[key]
        if not isinstance(found, DocumentBlob):
            return found
        return "intact" if passage_holds(found.text, context) else "source_changed"

    def _fetch_now(
        self, key: _DocumentKey, corpus: CorpusRef | None
    ) -> DocumentBlob | EvidenceStatus:
        """A document as its provider holds it now, or why it cannot be read."""
        if corpus is None:
            return "not_verifiable"
        _, document_id, version = key
        log_ctx = {
            "component": "policy_gate",
            "corpus_id": corpus.corpus_id,
            "provider": corpus.provider,
            "document_id": document_id,
            "version": version,
        }
        try:
            provider = self._providers.get(corpus.provider)
        except Exception as err:
            log.warning(
                "policy_gate.verify_provider_unavailable", error=str(err), **log_ctx
            )
            return "source_unavailable"
        if not isinstance(provider, DocumentFetcher):
            return "not_verifiable"
        try:
            return _check_document(
                provider.fetch_document(corpus, document_id, version), key
            )
        except DocumentNotHeldError:
            return "source_changed"
        except Exception as err:
            log.warning(
                "policy_gate.verify_document_unavailable", error=str(err), **log_ctx
            )
            return "source_unavailable"

    def close(self) -> None:
        """Release the providers this gate built and the handles it owns."""
        self._providers.close()
        for resource in self._resources:
            resource.close()

    def serves(self, reasoner_id: str) -> bool:
        """Whether this gate can serve ``reasoner_id`` at all.

        The gate authorizes each reasoner against the profile it derived
        from that reasoner's own prompts and corpus — its plugin's, or its
        directory under the asset root once ``validate_binding`` admitted it
        — so a reasoner it holds no profile for would have every submission
        refused. The framework asks after the binding was admitted
        (``ReasonerScope``): the derived read of "has this gate accepted a
        binding for this reasoner?".

        Args:
            reasoner_id: A reasoner whose registration binds this gate.

        Returns:
            ``True`` when the gate was built without profiles (it authorizes
            everyone) or holds one for ``reasoner_id``.
        """
        profiles = self._profiles
        return profiles is None or reasoner_id in profiles

    def validate_binding(self, reasoner_id: str, config: object) -> None:
        """Admit ``reasoner_id``'s binding to this gate, or refuse it (DR-33, DR-34).

        The framework asks for every registration that binds the gate: at
        startup, and when a registration is written over the API. The
        binding's configuration must be a ``PolicyBindingConfig`` naming at
        least one corpus; every corpus's provider must be installed, must
        describe the corpus (the reference provider: the corpus is loaded),
        and must supply at least the provenance the binding asks for. These
        checks change nothing. Then, a reasoner the gate already serves (a
        composed plugin's, or one admitted earlier) is admitted as it is:
        its assets are not read again. Any other reasoner is admitted from
        its directory under the asset root: its prompt templates join the
        prompt registry and its profile is added, both or neither.
        Add-only: template ids are global and immutable, so a template id
        the deployment already registers refuses the binding, and nothing is
        ever removed. The gate keeps no copy of the binding: it reads the
        binding from every request (``GateRequest.binding_config``).

        Args:
            reasoner_id: The reasoner whose registration binds this gate.
            config: The binding's ``config``: the ``PolicyBindingConfig``
                the framework validated with the plugin's
                ``binding_config_model``, or the raw mapping.

        Raises:
            GateConfigError: The configuration is not a valid
                ``PolicyBindingConfig``; a provider is not installed, cannot
                be built, cannot describe a corpus, or cannot supply its
                ``min_provenance``; or the reasoner has no plugin composed
                and the asset root is not set, or does not hold its prompts
                and corpus, or they do not load, or one of its template ids
                is registered already.
        """
        binding = self._binding(config)
        for corpus in binding.corpora:
            try:
                provider = self._providers.get(corpus.provider)
            except LookupError as err:
                raise GateConfigError(str(err)) from err
            except Exception as err:  # a provider that fails to build
                msg = f"retrieval provider {corpus.provider!r} cannot be built: {err}"
                raise GateConfigError(msg) from err
            try:
                state = provider.describe(corpus)
            except Exception as err:  # any provider error refuses the binding
                msg = (
                    f"corpus {corpus.corpus_id!r} cannot be described by provider "
                    f"{corpus.provider!r}: {err}"
                )
                raise GateConfigError(msg) from err
            if not provenance_at_least(state.provenance_level, corpus.min_provenance):
                msg = (
                    f"provider {corpus.provider!r} supplies {state.provenance_level!r} "
                    f"provenance for corpus {corpus.corpus_id!r}, below the binding's "
                    f"min_provenance {corpus.min_provenance!r}"
                )
                raise GateConfigError(msg)
        self._admit_reasoner(reasoner_id)

    @staticmethod
    def _binding(config: object) -> PolicyBindingConfig:
        """``config`` as a ``PolicyBindingConfig``, or a ``GateConfigError``."""
        if isinstance(config, PolicyBindingConfig):
            return config
        try:
            return PolicyBindingConfig.model_validate(config)
        except ValidationError as err:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in e['loc']) or '<root>'}: {e['msg']}"
                for e in err.errors(include_url=False)
            )
            msg = f"the policy gate's binding configuration is invalid: {problems}"
            raise GateConfigError(msg) from err

    def _admit_reasoner(self, reasoner_id: str) -> None:
        """Serve ``reasoner_id`` from its plugin, or admit it from the asset root."""
        if self._profiles is None or reasoner_id in self._profiles:
            return
        with self._admission:
            if reasoner_id in self._profiles:
                return
            if self._assets_root is None:
                msg = (
                    f"reasoner {reasoner_id!r} has no plugin composed in this "
                    "deployment, and POLICY_RAG_GATE_ASSETS_ROOT is not set: "
                    "ship its prompts and corpus under "
                    "<root>/<reasoner_id>/{prompts,corpus}"
                )
                raise GateConfigError(msg)
            profile, templates = load_root_assets(self._assets_root, reasoner_id)
            add = getattr(self._registry, "add", None)
            if add is None:
                msg = (
                    "this gate's prompt registry cannot register templates "
                    f"after the gate is built, so reasoner {reasoner_id!r} "
                    "cannot be admitted"
                )
                raise GateConfigError(msg)
            try:
                add(templates)
            except ValueError as err:
                raise GateConfigError(str(err)) from err
            self._profiles = {**self._profiles, reasoner_id: profile}
        log.info(
            "policy_gate.reasoner_admitted",
            component="policy_gate",
            reasoner_id=reasoner_id,
            source="assets_root",
            prompt_templates=sorted(profile.template_ids),
            jurisdictions=sorted(profile.jurisdictions),
        )

    def authorize_config(self, config: object, *, reasoner_id: str) -> None:
        """Check that ``reasoner_id`` may submit ``config`` to this gate.

        The framework calls this at intake, after validating ``gate_config``
        against ``PolicyGateConfig``, on every route — so a recorded decision
        never carries a gate context naming another reasoner's template or a
        jurisdiction outside the submitter's corpus.

        Args:
            config: The validated ``PolicyGateConfig`` (a raw mapping is
                validated here).
            reasoner_id: The submitting reasoner.

        Raises:
            ValueError: The reasoner is not one this gate was built for, or
                ``config`` falls outside its profile.
        """
        profiles = self._profiles
        if profiles is None:
            return
        typed = (
            config
            if isinstance(config, PolicyGateConfig)
            else PolicyGateConfig.model_validate(config)
        )
        profile = profiles.get(reasoner_id)
        if profile is None:
            msg = (
                f"reasoner {reasoner_id!r} is not one this gate serves: "
                f"{sorted(profiles)}"
            )
            raise ValueError(msg)
        profile.authorize(typed)

    async def evaluate(self, request: GateRequest) -> GateOutput:
        """Retrieve policy evidence for the request and ask the LLM for a verdict.

        Args:
            request: Framework-built request. ``gate_context.gate_config``
                is a ``PolicyGateConfig``; ``binding_config`` is the
                reasoner's ``PolicyBindingConfig`` (the corpora to read).

        Returns:
            The universal ``GateOutput`` (``gate_id="policy"``,
            ``deterministic=False``).

        Raises:
            pydantic.ValidationError: ``gate_config`` is not a
                ``PolicyGateConfig`` (the framework rejects that at intake,
                so only a direct caller sees it).
            GateConfigError: ``binding_config`` is not a
                ``PolicyBindingConfig`` (admission refuses that, so only a
                direct caller sees it).
            KeyError: ``template_id`` is not registered.
            ProviderContractError: A provider's answer breaks the evidence
                contract.
            Exception: Whatever a provider raises (an outage propagates to
                the caller unchanged).
        """
        t0 = time.perf_counter()
        submission = request.submission
        log_ctx = {
            "component": "policy_gate",
            "decision_id": request.decision_id,
            "event_id": submission.event_id,
            "model": self._model_version_label,
        }

        config = PolicyGateConfig.model_validate(request.gate_context.gate_config or {})
        binding = self._binding(request.binding_config)
        template_vars = dict(config.template_vars)
        query = config.retrieval_query

        template = self._registry.get(config.template_id)
        layout = template.evidence_layout
        prompt_snapshot = PromptSnapshot(
            template_id=template.template_id,
            version=template.version,
            template_text=template.template_text,
            system_text=template.system_text,
        )

        def _snapshot(
            *,
            retrieval_query: str,
            queries: list[CorpusQuery],
            contexts: list[RetrievedContext],
            dedup_merged: int,
            rendered_prompt: str,
        ) -> PolicyGateSnapshot:
            return PolicyGateSnapshot(
                retrieval_query=retrieval_query,
                corpus_queries=queries,
                contexts=contexts,
                evidence_layout=layout,
                dedup_merged=dedup_merged,
                index_version=self._index_version,
                model_version=self._model_version_label,
                prompt_template_id=template.template_id,
                prompt_template_version=template.version,
                rendered_prompt=rendered_prompt,
                prompt_snapshot=prompt_snapshot,
                template_vars=template_vars,
            )

        def _failed(
            failure: PolicyGateFailure,
            *,
            snapshot: PolicyGateSnapshot,
            contexts: list[RetrievedContext],
            retrieval_ms: float,
        ) -> GateOutput:
            return _policy_gate_output(
                verdict=None,
                contexts=contexts,
                snapshot=snapshot,
                extra=PolicyGateExtra(retrieval_ms=retrieval_ms, failure=failure),
            )

        # --- Retrieval (every corpus the binding names) ---
        if query is None or not query.strip():
            log.error("policy_gate.missing_retrieval_query", **log_ctx)
            return _failed(
                "missing_retrieval_query",
                snapshot=_snapshot(
                    retrieval_query="",
                    queries=[],
                    contexts=[],
                    dedup_merged=0,
                    rendered_prompt="",
                ),
                contexts=[],
                retrieval_ms=0.0,
            )
        t_retrieval = time.perf_counter()
        evidence = retrieve_evidence(
            self._providers,
            binding,
            query,
            jurisdictions=config.jurisdictions,
            risk_tier=config.risk_tier,
        )
        queries, contexts = evidence.queries, evidence.contexts
        dedup_merged = evidence.dedup_merged
        retrieval_ms = duration_ms(t_retrieval)

        # --- Escrow: each cited document, once, before anything is built ---
        if self._escrow is not None and contexts:
            contexts = await asyncio.to_thread(
                escrow_documents,
                self._providers,
                queries,
                contexts,
                self._escrow,
                decision_id=request.decision_id,
            )

        # --- Render prompt ---
        try:
            rendered = _render_prompt(
                template.template_text,
                template_vars,
                render_evidence(contexts, layout),
            )
        except KeyError as exc:
            log.error("policy_gate.render_error", error=str(exc), **log_ctx)
            return _failed(
                "render_error",
                snapshot=_snapshot(
                    retrieval_query=query,
                    queries=queries,
                    contexts=contexts,
                    dedup_merged=dedup_merged,
                    rendered_prompt="",
                ),
                contexts=contexts,
                retrieval_ms=retrieval_ms,
            )

        snapshot = _snapshot(
            retrieval_query=query,
            queries=queries,
            contexts=contexts,
            dedup_merged=dedup_merged,
            rendered_prompt=rendered,
        )

        # --- LLM invocation via SDK-agnostic client ---
        # The system message belongs to the template (and so to the reasoner
        # that ships it); the gate adds no wording of its own. The response
        # schema follows the template's layout: a template written for tiered
        # evidence asks for the tiered report.
        response_format: type[PolicyGateVerdict] = (
            TieredPolicyGateVerdict if layout == "tiered_blocks" else PolicyGateVerdict
        )
        try:
            result = await self._client.complete_structured(
                system=template.system_text,
                user=rendered,
                response_format=response_format,
            )
        except Exception as exc:
            # Network errors, refusals, schema-validation failures all
            # surface here. Produce the verdict=None output enforcement
            # Tier 1 routes to HOLD.
            log.error("policy_gate.llm_error", error=str(exc), **log_ctx)
            return _failed(
                "llm_error",
                snapshot=snapshot,
                contexts=contexts,
                retrieval_ms=retrieval_ms,
            )

        verdict = result.parsed
        universal = verdict.universal()
        extra_fields: dict[str, object] = {}
        if isinstance(verdict, TieredPolicyGateVerdict):
            held, hold = _held(verdict)
            extra_fields = {
                "governing_corpus_id": verdict.governing_corpus_id,
                "conflict_detected": verdict.conflict_detected,
                "evidence_sufficient": verdict.evidence_sufficient,
            }
            if hold is not None:
                extra_fields |= {
                    "protocol_hold": hold,
                    "verdict_as_returned": universal,
                }
                universal = held
        log.info(
            "policy_gate.evaluated",
            permitted_actions=[a.value for a in universal.permitted_actions],
            confidence=universal.confidence,
            corpora=[q.corpus_ref.corpus_id for q in queries],
            ranking_applied=[q.ranking_applied for q in queries],
            contexts=len(contexts),
            dedup_merged=dedup_merged,
            protocol_hold=extra_fields.get("protocol_hold"),
            duration_ms=duration_ms(t0),
            **log_ctx,
        )
        return _policy_gate_output(
            verdict=universal,
            contexts=contexts,
            snapshot=snapshot,
            extra=PolicyGateExtra.model_validate(
                {
                    "rationale": verdict.rationale,
                    "citations": list(verdict.citations),
                    "response_text": None,
                    "token_cost": _build_token_cost(
                        result.usage, self._model_version_label
                    ),
                    "retrieval_ms": retrieval_ms,
                    **extra_fields,
                }
            ),
        )
