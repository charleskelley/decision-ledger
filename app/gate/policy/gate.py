"""LLM-backed policy gate orchestrator — prompt rendering, LLM invocation, validation.

PolicyGate renders the prompt template, calls the LLM through the
SDK-agnostic ``LLMClient`` (DR-23), and returns a ``GateResult``
carrying typed ``PolicyGateInput`` and ``PolicyGateOutput``.

Strict structured output is enforced at the SDK layer — the LLMClient
returns a Pydantic-validated ``PolicyGateVerdict`` directly. The gate
does no manual JSON parsing on success.

Any failure (template render error, LLMClient error, schema-validation
refusal at the SDK boundary) produces a ``GateResult`` whose
``gate_output.verdict`` is ``None``. Enforcement Tier 1 routes
schema-failure decisions to HOLD deterministically.

Usage::

    from openai import AsyncOpenAI
    from app.llm.openai import OpenAILLMClient
    from app.gate.policy import PolicyGate, YamlPromptRegistry

    client = OpenAILLMClient(client=AsyncOpenAI())
    gate = PolicyGate(client=client, prompt_registry=YamlPromptRegistry())
    result = await gate.evaluate(
        obs, snippets, decision_id="dec-abc123", corpus_version="corpus-v1"
    )
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from core.gate.policy import (
    PolicyGateInput,
    PolicyGateOutput,
    PolicyGateVerdict,
    PromptSnapshot,
    TokenCost,
)

if TYPE_CHECKING:
    from core.gate.policy import PromptRegistry, PromptTemplate
    from core.llm import LLMClient
    from core.observation import Observation
    from core.snippet import RetrievedSnippet

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Gate-level system prompt — minimal framing for the policy-evaluation task.
# Per-domain rubric and event context flow in via the user-message template.
# ---------------------------------------------------------------------------

_GATE_SYSTEM_PROMPT = (
    "You evaluate authentication risk for the policy gate. "
    "Respond with a PolicyGateVerdict that conforms strictly to the schema."
)


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateResult:
    """Output of a single policy gate evaluation.

    Carries the typed ``PolicyGateInput`` and ``PolicyGateOutput``
    contracts the bundle persists, plus operational ``latency_ms``.

    Args:
        gate_input: Concrete input artifacts (typed ``PolicyGateInput``).
        gate_output: Concrete output artifacts.
            ``gate_output.verdict`` is ``None`` when the LLM call or
            schema validation failed; ``gate_output.response_text`` is
            ``None`` in that case (the SDK's strict parser does not
            surface unparseable text).
        latency_ms: Wall-clock time for the full ``evaluate()`` call in
            milliseconds (including render + LLM call + bookkeeping).
    """

    gate_input: PolicyGateInput
    gate_output: PolicyGateOutput
    latency_ms: float


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


def _build_token_cost(usage, model: str) -> TokenCost:
    """Translate a neutral ``TokenUsage`` into the audit-side ``TokenCost``."""
    return TokenCost(
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        cost_usd=round(usage.cost_usd if usage.cost_usd is not None else 0.0, 6),
        model=model,
    )


def _build_gate_input(
    *,
    model_version: str,
    template: PromptTemplate,
    template_vars: dict[str, str],
    corpus_version: str,
    rendered_prompt: str,
    prompt_snapshot: PromptSnapshot,
) -> PolicyGateInput:
    """Construct the typed PolicyGateInput record from invocation context."""
    return PolicyGateInput(
        model_version=model_version,
        prompt_template_id=template.template_id,
        prompt_template_version=template.version,
        corpus_version=corpus_version,
        rendered_prompt=rendered_prompt,
        prompt_snapshot=prompt_snapshot,
        template_vars=template_vars,
    )


def _build_gate_output(
    *,
    verdict: PolicyGateVerdict | None,
    token_cost: TokenCost | None,
) -> PolicyGateOutput:
    """Construct the typed PolicyGateOutput record."""
    return PolicyGateOutput(
        verdict=verdict,
        response_text=None,
        token_cost=token_cost,
    )


# ---------------------------------------------------------------------------
# PolicyGate
# ---------------------------------------------------------------------------


class PolicyGate:
    """LLM-backed policy gate orchestrator.

    Wraps an ``LLMClient`` (DR-23) in a structured evaluation loop. Any
    failure (network error, timeout, refusal, schema-validation failure
    at the SDK boundary) produces a ``GateResult`` whose
    ``gate_output.verdict`` is ``None`` so enforcement can apply Tier 1
    (schema failure → HOLD) deterministically.

    Args:
        client: SDK-agnostic ``LLMClient`` adapter. Decouples gate
            behavior from the underlying LLM provider — see DR-23.
        prompt_registry: Registry for resolving versioned prompt
            templates.
        model_version_label: Free-form label recorded into
            ``PolicyGateInput.model_version``. Defaults to
            ``client.model`` when the client exposes a ``model``
            property; otherwise an opaque ``"<llm-client>"`` placeholder.
    """

    def __init__(
        self,
        *,
        client: LLMClient,
        prompt_registry: PromptRegistry,
        model_version_label: str | None = None,
    ) -> None:
        """Initialize the policy gate with an LLMClient and prompt registry."""
        self._client = client
        self._registry = prompt_registry
        self._model_version_label = (
            model_version_label
            if model_version_label is not None
            else getattr(client, "model", "<llm-client>")
        )

    async def evaluate(
        self,
        obs: Observation,
        snippets: list[RetrievedSnippet],
        *,
        decision_id: str,
        corpus_version: str,
    ) -> GateResult:
        """Evaluate an observation against retrieved policy evidence.

        Renders the prompt template, invokes the LLM through ``LLMClient``,
        and returns a ``GateResult`` carrying typed
        ``PolicyGateInput`` / ``PolicyGateOutput``.

        Args:
            obs: Assembled observation with ``gate_context`` populated.
            snippets: Policy chunks from the retriever for this observation.
            decision_id: Decision ID for log context and audit tracing.
            corpus_version: Version label of the retrieval corpus this
                evaluation consumed. Recorded in
                ``gate_input.corpus_version``.

        Returns:
            ``GateResult`` carrying typed input/output contracts and
            wall-clock latency.
        """
        t0 = time.perf_counter()
        log_ctx = {
            "component": "policy_gate",
            "decision_id": decision_id,
            "event_id": obs.event_id,
            "model": self._model_version_label,
        }

        config = obs.gate_context.gate_config or {}
        template_id = config.get("template_id", "")
        template_vars: dict[str, str] = config.get("template_vars", {})

        template = self._registry.get(template_id)
        prompt_snapshot = PromptSnapshot(
            template_id=template.template_id,
            version=template.version,
            template_text=template.template_text,
        )

        # --- Render prompt ---
        try:
            rendered = _render_prompt(
                template.template_text,
                template_vars,
                snippets,
            )
        except KeyError as exc:
            log.error("policy_gate.render_error", error=str(exc), **log_ctx)
            gate_input = _build_gate_input(
                model_version=self._model_version_label,
                template=template,
                template_vars=template_vars,
                corpus_version=corpus_version,
                rendered_prompt="",
                prompt_snapshot=prompt_snapshot,
            )
            gate_output = _build_gate_output(verdict=None, token_cost=None)
            return GateResult(
                gate_input=gate_input,
                gate_output=gate_output,
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            )

        # --- LLM invocation via SDK-agnostic client ---
        try:
            result = await self._client.complete_structured(
                system=_GATE_SYSTEM_PROMPT,
                user=rendered,
                response_format=PolicyGateVerdict,
            )
        except Exception as exc:
            # Network errors, refusals, schema-validation failures all
            # surface here. Produce the verdict=None record enforcement
            # Tier 1 routes to HOLD.
            log.error("policy_gate.llm_error", error=str(exc), **log_ctx)
            gate_input = _build_gate_input(
                model_version=self._model_version_label,
                template=template,
                template_vars=template_vars,
                corpus_version=corpus_version,
                rendered_prompt=rendered,
                prompt_snapshot=prompt_snapshot,
            )
            gate_output = _build_gate_output(verdict=None, token_cost=None)
            return GateResult(
                gate_input=gate_input,
                gate_output=gate_output,
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            )

        verdict = result.parsed
        token_cost = _build_token_cost(result.usage, self._model_version_label)
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)

        log.info(
            "policy_gate.evaluated",
            permitted_actions=[a.value for a in verdict.permitted_actions],
            confidence=verdict.confidence,
            **log_ctx,
        )

        gate_input = _build_gate_input(
            model_version=self._model_version_label,
            template=template,
            template_vars=template_vars,
            corpus_version=corpus_version,
            rendered_prompt=rendered,
            prompt_snapshot=prompt_snapshot,
        )
        gate_output = _build_gate_output(verdict=verdict, token_cost=token_cost)
        return GateResult(
            gate_input=gate_input,
            gate_output=gate_output,
            latency_ms=latency_ms,
        )
