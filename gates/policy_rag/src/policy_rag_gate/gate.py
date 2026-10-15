"""LLM-backed policy gate — retrieval, prompt rendering, LLM invocation, validation.

``PolicyGate`` is the reference ``Gate`` (DR-28). Given a ``GateRequest`` it
runs hybrid retrieval over the submitting reasoner's corpus, renders the
reasoner's prompt template with the retrieved snippets, calls the LLM
through the SDK-agnostic ``LLMClient`` (DR-23) with the template's own
``system_text`` as the system message, and returns the universal
``GateOutput``:

- ``verdict`` — the universal ``GateVerdict`` part of the model's
  ``PolicyGateVerdict`` (``None`` on any failure);
- ``evidence`` — one ``EvidenceRef`` per retrieved snippet, carrying the
  snippet's jurisdiction so enforcement's jurisdiction-conflict tier sees
  exactly the retrieved set;
- ``evidence_snapshot`` — a ``PolicyGateSnapshot``: the retrieval (query,
  results, path, index version) and the exact prompt (model, template
  identity and snapshot, substitutions, rendered text);
- ``extra`` — a ``PolicyGateExtra``: rationale, citations, token cost,
  retrieval latency, and the failing stage when there is no verdict.

The gate carries no domain wording: everything the model reads comes from
the reasoner's template file and the retrieved snippets. Everything it needs
from the reasoner comes from ``gate_config``, typed by ``PolicyGateConfig``
(the gate's ``config_model``, validated by the framework at intake):
``template_id``, ``template_vars``, ``retrieval_query``, ``jurisdictions``,
``risk_tier``, and ``corpus_version``.

Who may send what is the gate's call, not the registration's (DR-28): when
built with per-reasoner ``ReasonerProfile`` objects, ``authorize_config``
— which the framework calls at intake — accepts only templates the
submitting reasoner ships and jurisdictions its corpus covers.

Strict structured output is enforced at the SDK layer — the ``LLMClient``
returns a Pydantic-validated ``PolicyGateVerdict`` directly. Any failure to
reach a verdict (no retrieval query, template render error, LLM error or
SDK-boundary schema refusal) produces ``verdict=None``, which enforcement
Tier 1 routes to HOLD deterministically. An unknown ``template_id`` or a
retrieval-store error propagates to the caller, as before the seam.

Usage::

    gate = PolicyGate(client=client, prompt_registry=registry, retriever=retriever)
    output = await gate.evaluate(request)
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Protocol

import structlog

from app.monitoring import duration_ms
from core.gate import EvidenceRef, GateOutput
from policy_rag_gate.config import PolicyGateConfig
from policy_rag_gate.models import (
    PolicyGateExtra,
    PolicyGateSnapshot,
    PolicyGateVerdict,
    PromptSnapshot,
    TokenCost,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from core.gate_request import GateRequest
    from core.llm import LLMClient
    from core.llm.client import TokenUsage
    from policy_rag_gate.config import ReasonerProfile
    from policy_rag_gate.models import PolicyGateFailure, PromptRegistry
    from policy_rag_gate.models.snippet import RetrievedSnippet
    from policy_rag_gate.retriever import PolicyRetriever

log = structlog.get_logger(__name__)

#: The policy gate's identifier — what ``GateContext.gate_id`` names.
POLICY_GATE_ID = "policy"

#: Snippets retrieved per invocation.
DEFAULT_TOP_K = 5


class _Closeable(Protocol):
    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Private helpers (pure — no I/O; testable without infrastructure)
# ---------------------------------------------------------------------------


def _render_snippets(snippets: list[RetrievedSnippet]) -> str:
    """Format retrieved policy snippets into prompt-ready text."""
    if not snippets:
        return "(no policy evidence retrieved)"
    parts = []
    for i, s in enumerate(snippets, 1):
        parts.append(
            f"[{i}] {s.title} (v{s.version}) — {s.jurisdiction}\n"
            f"Section: {s.section_path}\n"
            f"{s.text}"
        )
    return "\n\n".join(parts)


def _render_prompt(
    template_str: str,
    template_vars: dict[str, str],
    snippets: list[RetrievedSnippet],
) -> str:
    """Render a prompt template with domain variables and policy snippets.

    Injects policy snippets first (with brace-escaping to prevent
    ``str.format()`` from misinterpreting any ``{ }`` characters in
    snippet text as placeholders), then substitutes all domain
    ``template_vars``.

    Raises:
        KeyError: If any ``{placeholder}`` in ``template_str`` is absent
            from ``template_vars`` — indicates a mismatch in the domain
            assembler.
    """
    snippets_text = _render_snippets(snippets)
    snippets_escaped = snippets_text.replace("{", "{{").replace("}", "}}")
    interim = template_str.replace("{policy_snippets}", snippets_escaped)
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


def _evidence(snippets: Sequence[RetrievedSnippet]) -> tuple[EvidenceRef, ...]:
    """One ``EvidenceRef`` per retrieved snippet, carrying its jurisdiction.

    Enforcement's jurisdiction-conflict tier reads the union of these
    jurisdictions, so it sees exactly the retrieved set it saw when the
    snippets were a bundle layer.
    """
    return tuple(
        EvidenceRef(
            source_id=s.document_id,
            locator=s.section_path,
            jurisdictions=frozenset({s.jurisdiction})
            if s.jurisdiction
            else frozenset(),
        )
        for s in snippets
    )


def _policy_gate_output(
    *,
    verdict: PolicyGateVerdict | None,
    snippets: Sequence[RetrievedSnippet],
    snapshot: PolicyGateSnapshot,
    extra: PolicyGateExtra,
) -> GateOutput:
    """Assemble the universal ``GateOutput`` the policy gate returns."""
    return GateOutput(
        gate_id=POLICY_GATE_ID,
        deterministic=False,
        verdict=verdict.universal() if verdict is not None else None,
        evidence=_evidence(snippets),
        evidence_snapshot=snapshot.model_dump(mode="json"),
        extra=extra.model_dump(mode="json"),
    )


# ---------------------------------------------------------------------------
# PolicyGate
# ---------------------------------------------------------------------------


class PolicyGate:
    """LLM-backed policy gate: hybrid retrieval plus a structured LLM verdict.

    Satisfies ``app.gates.Gate`` and its optional ``ConfigAuthorizer``
    capability. Owns its retriever, prompt registry, LLM client, and
    per-reasoner profiles; ``close()`` releases the infrastructure handles it
    was given.

    Args:
        client: SDK-agnostic ``LLMClient`` adapter. Decouples gate
            behavior from the underlying LLM provider — see DR-23.
        prompt_registry: Registry for resolving versioned prompt
            templates (every composed reasoner's ``prompts_dir``).
        retriever: Hybrid retriever over the gate's policy corpus stores.
        top_k: Snippets to retrieve per invocation.
        index_version: Version label of the retrieval index, recorded in
            every snapshot.
        model_version_label: Free-form label recorded as the snapshot's
            ``model_version``. Defaults to ``client.model`` when the client
            exposes a ``model`` property; otherwise an opaque
            ``"<llm-client>"`` placeholder.
        profiles: What each composed reasoner may submit, keyed by
            ``reasoner_id`` (the plugin derives them from each reasoner's
            prompts and corpus). ``None`` authorizes every submission — a
            gate assembled by hand for a test or a fault-injection run.
        resources: Infrastructure handles the gate owns (connections,
            clients) and closes on ``close()``.
    """

    def __init__(
        self,
        *,
        client: LLMClient,
        prompt_registry: PromptRegistry,
        retriever: PolicyRetriever,
        top_k: int = DEFAULT_TOP_K,
        index_version: str = "unknown",
        model_version_label: str | None = None,
        profiles: Mapping[str, ReasonerProfile] | None = None,
        resources: Sequence[_Closeable] = (),
    ) -> None:
        """Initialize the policy gate with its client, registry, and retriever."""
        self._client = client
        self._registry = prompt_registry
        self._retriever = retriever
        self._top_k = top_k
        self._index_version = index_version
        self._model_version_label = (
            model_version_label
            if model_version_label is not None
            else getattr(client, "model", "<llm-client>")
        )
        self._profiles = dict(profiles) if profiles is not None else None
        self._resources = tuple(resources)

    @property
    def client(self) -> LLMClient:
        """The LLM client the gate calls."""
        return self._client

    @property
    def prompt_registry(self) -> PromptRegistry:
        """The prompt registry the gate renders from."""
        return self._registry

    @property
    def retriever(self) -> PolicyRetriever:
        """The hybrid retriever the gate queries."""
        return self._retriever

    @property
    def profiles(self) -> Mapping[str, ReasonerProfile] | None:
        """Per-reasoner profiles the gate authorizes against (``None``: any)."""
        return self._profiles

    def close(self) -> None:
        """Release the infrastructure handles this gate owns."""
        for resource in self._resources:
            resource.close()

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
        if self._profiles is None:
            return
        typed = (
            config
            if isinstance(config, PolicyGateConfig)
            else PolicyGateConfig.model_validate(config)
        )
        profile = self._profiles.get(reasoner_id)
        if profile is None:
            msg = (
                f"reasoner {reasoner_id!r} is not one this gate was built for: "
                f"{sorted(self._profiles)}"
            )
            raise ValueError(msg)
        profile.authorize(typed)

    async def evaluate(self, request: GateRequest) -> GateOutput:
        """Retrieve policy evidence for the request and ask the LLM for a verdict.

        Args:
            request: Framework-built request. ``gate_context.gate_config``
                is a ``PolicyGateConfig``;
                ``submission.reasoner_context.reasoner_id`` scopes
                retrieval to the submitting reasoner's corpus.

        Returns:
            The universal ``GateOutput`` (``gate_id="policy"``,
            ``deterministic=False``).

        Raises:
            pydantic.ValidationError: ``gate_config`` is not a
                ``PolicyGateConfig`` (the framework rejects that at intake,
                so only a direct caller sees it).
            KeyError: ``template_id`` is not registered.
            Exception: Whatever the retrieval stores raise (an outage
                propagates to the caller unchanged).
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
        template_vars = dict(config.template_vars)
        corpus_version = config.corpus_version
        query = config.retrieval_query

        template = self._registry.get(config.template_id)
        prompt_snapshot = PromptSnapshot(
            template_id=template.template_id,
            version=template.version,
            template_text=template.template_text,
            system_text=template.system_text,
        )

        def _snapshot(
            *,
            retrieval_query: str,
            snippets: list[RetrievedSnippet],
            retrieval_path: str,
            rendered_prompt: str,
        ) -> PolicyGateSnapshot:
            return PolicyGateSnapshot(
                retrieval_query=retrieval_query,
                retrieval_results=snippets,
                retrieval_path=retrieval_path,
                index_version=self._index_version,
                model_version=self._model_version_label,
                prompt_template_id=template.template_id,
                prompt_template_version=template.version,
                corpus_version=corpus_version,
                rendered_prompt=rendered_prompt,
                prompt_snapshot=prompt_snapshot,
                template_vars=template_vars,
            )

        def _failed(
            failure: PolicyGateFailure,
            *,
            snapshot: PolicyGateSnapshot,
            snippets: list[RetrievedSnippet],
            retrieval_ms: float,
        ) -> GateOutput:
            return _policy_gate_output(
                verdict=None,
                snippets=snippets,
                snapshot=snapshot,
                extra=PolicyGateExtra(retrieval_ms=retrieval_ms, failure=failure),
            )

        # --- Retrieval (scoped to the submitting reasoner's corpus) ---
        if query is None or not query.strip():
            log.error("policy_gate.missing_retrieval_query", **log_ctx)
            return _failed(
                "missing_retrieval_query",
                snapshot=_snapshot(
                    retrieval_query="",
                    snippets=[],
                    retrieval_path="skipped",
                    rendered_prompt="",
                ),
                snippets=[],
                retrieval_ms=0.0,
            )
        t_retrieval = time.perf_counter()
        snippets, retrieval_path = self._retriever.retrieve(
            query=query,
            k=self._top_k,
            reasoner_id=submission.reasoner_context.reasoner_id,
            jurisdictions=list(config.jurisdictions) or None,
            risk_tier=config.risk_tier,
        )
        retrieval_ms = duration_ms(t_retrieval)

        # --- Render prompt ---
        try:
            rendered = _render_prompt(template.template_text, template_vars, snippets)
        except KeyError as exc:
            log.error("policy_gate.render_error", error=str(exc), **log_ctx)
            return _failed(
                "render_error",
                snapshot=_snapshot(
                    retrieval_query=query,
                    snippets=snippets,
                    retrieval_path=retrieval_path,
                    rendered_prompt="",
                ),
                snippets=snippets,
                retrieval_ms=retrieval_ms,
            )

        snapshot = _snapshot(
            retrieval_query=query,
            snippets=snippets,
            retrieval_path=retrieval_path,
            rendered_prompt=rendered,
        )

        # --- LLM invocation via SDK-agnostic client ---
        # The system message belongs to the template (and so to the reasoner
        # that ships it); the gate adds no wording of its own.
        try:
            result = await self._client.complete_structured(
                system=template.system_text,
                user=rendered,
                response_format=PolicyGateVerdict,
            )
        except Exception as exc:
            # Network errors, refusals, schema-validation failures all
            # surface here. Produce the verdict=None output enforcement
            # Tier 1 routes to HOLD.
            log.error("policy_gate.llm_error", error=str(exc), **log_ctx)
            return _failed(
                "llm_error",
                snapshot=snapshot,
                snippets=snippets,
                retrieval_ms=retrieval_ms,
            )

        verdict = result.parsed
        log.info(
            "policy_gate.evaluated",
            permitted_actions=[a.value for a in verdict.permitted_actions],
            confidence=verdict.confidence,
            retrieval_path=retrieval_path,
            snippets=len(snippets),
            duration_ms=duration_ms(t0),
            **log_ctx,
        )
        return _policy_gate_output(
            verdict=verdict,
            snippets=snippets,
            snapshot=snapshot,
            extra=PolicyGateExtra(
                rationale=verdict.rationale,
                citations=list(verdict.citations),
                response_text=None,
                token_cost=_build_token_cost(result.usage, self._model_version_label),
                retrieval_ms=retrieval_ms,
            ),
        )
