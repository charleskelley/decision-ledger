"""LLM-backed policy gate orchestrator — prompt rendering, LLM invocation, validation.

PolicyGate renders the prompt template, calls the OpenAI API, validates
the structured JSON response against ``PolicyGateVerdict``, and returns
a ``GateResult`` carrying typed ``PolicyGateInput`` and
``PolicyGateOutput``.

Any failure (template render error, API error, JSON parse failure,
schema validation failure) produces a ``GateResult`` whose
``gate_output.verdict`` is ``None``. The enforcement layer handles that
via Tier 1 (schema failure → HOLD).

Usage::

    from openai import OpenAI
    from app.gate.policy import PolicyGate, YamlPromptRegistry

    gate = PolicyGate(OpenAI(), YamlPromptRegistry())
    result = gate.evaluate(
        obs, snippets, decision_id="dec-abc123", corpus_version="corpus-v1"
    )
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from pydantic import ValidationError

from core.gate.policy import (
    PolicyGateInput,
    PolicyGateOutput,
    PolicyGateVerdict,
    PromptSnapshot,
    TokenCost,
)

if TYPE_CHECKING:
    from openai import OpenAI

    from core.gate.policy import PromptRegistry, PromptTemplate
    from core.observation import Observation
    from core.snippet import RetrievedSnippet

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Approximate GPT-4o pricing (USD per 1M tokens, as of 2025-04).
# Used for cost estimation in TokenCost — not billing.
# ---------------------------------------------------------------------------

_INPUT_COST_PER_1M_USD: float = 2.50
_OUTPUT_COST_PER_1M_USD: float = 10.00


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
            ``gate_output.verdict`` is ``None`` when the LLM response
            could not be parsed or failed schema validation;
            ``gate_output.response_text`` carries forensic evidence in
            that case.
        latency_ms: Wall-clock time for the full ``evaluate()`` call in
            milliseconds.
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


def _parse_verdict(
    raw_response: str,
    *,
    decision_id: str,
) -> PolicyGateVerdict | None:
    """Parse and validate a raw LLM response string as PolicyGateVerdict.

    Returns ``None`` on any parse or validation error without raising.
    The caller logs the raw response; enforcement routes to HOLD via
    Tier 1.
    """
    try:
        data = json.loads(raw_response)
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning(
            "policy_gate.json_parse_error",
            component="policy_gate",
            decision_id=decision_id,
            error=str(exc),
        )
        return None

    try:
        return PolicyGateVerdict.model_validate(data, strict=False)
    except ValidationError as exc:
        log.warning(
            "policy_gate.schema_validation_error",
            component="policy_gate",
            decision_id=decision_id,
            error_count=exc.error_count(),
        )
        return None


def _compute_token_cost(usage: object, model: str) -> TokenCost:
    """Build a TokenCost from an OpenAI CompletionUsage object."""
    prompt_tokens: int = usage.prompt_tokens  # type: ignore[attr-defined]
    completion_tokens: int = usage.completion_tokens  # type: ignore[attr-defined]
    total_tokens: int = usage.total_tokens  # type: ignore[attr-defined]
    cost = (
        prompt_tokens / 1_000_000 * _INPUT_COST_PER_1M_USD
        + completion_tokens / 1_000_000 * _OUTPUT_COST_PER_1M_USD
    )
    return TokenCost(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost_usd=round(cost, 6),
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
    raw_response: str,
    token_cost: TokenCost | None,
) -> PolicyGateOutput:
    """Construct the typed PolicyGateOutput record."""
    return PolicyGateOutput(
        verdict=verdict,
        response_text=raw_response if raw_response else None,
        token_cost=token_cost,
    )


# ---------------------------------------------------------------------------
# PolicyGate
# ---------------------------------------------------------------------------


class PolicyGate:
    """LLM-backed policy gate orchestrator.

    Wraps the OpenAI API call in a structured evaluation loop. Any
    failure (network error, timeout, JSON parse failure, schema
    validation failure) produces a ``GateResult`` whose
    ``gate_output.verdict`` is ``None`` so enforcement can apply Tier 1
    (schema failure → HOLD) deterministically.

    Args:
        client: Authenticated OpenAI client instance.
        prompt_registry: Registry for resolving versioned prompt templates.
        model: OpenAI model to use for gate evaluations.
        timeout_secs: Per-request timeout forwarded to the OpenAI client.
    """

    def __init__(
        self,
        client: OpenAI,
        prompt_registry: PromptRegistry,
        *,
        model: str = "gpt-4o-2024-08-06",
        timeout_secs: float = 30.0,
    ) -> None:
        """Initialize the policy gate with an OpenAI client and prompt registry."""
        self._client = client
        self._registry = prompt_registry
        self._model = model
        self._timeout = timeout_secs

    def evaluate(
        self,
        obs: Observation,
        snippets: list[RetrievedSnippet],
        *,
        decision_id: str,
        corpus_version: str,
    ) -> GateResult:
        """Evaluate an observation against retrieved policy evidence.

        Renders the prompt template, calls the LLM, validates the
        response as ``PolicyGateVerdict``, and returns a ``GateResult``
        carrying typed ``PolicyGateInput`` / ``PolicyGateOutput``.

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
            "model": self._model,
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

        try:
            rendered = _render_prompt(
                template.template_text,
                template_vars,
                snippets,
            )
        except KeyError as exc:
            log.error("policy_gate.render_error", error=str(exc), **log_ctx)
            gate_input = _build_gate_input(
                model_version=self._model,
                template=template,
                template_vars=template_vars,
                corpus_version=corpus_version,
                rendered_prompt="",
                prompt_snapshot=prompt_snapshot,
            )
            gate_output = _build_gate_output(
                verdict=None, raw_response="", token_cost=None
            )
            return GateResult(
                gate_input=gate_input,
                gate_output=gate_output,
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            )

        raw_response = ""
        token_cost_obj: TokenCost | None = None
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": rendered}],
                response_format={"type": "json_object"},
                timeout=self._timeout,
            )
            raw_response = response.choices[0].message.content or ""
            if response.usage:
                token_cost_obj = _compute_token_cost(response.usage, self._model)
            log.debug(
                "policy_gate.llm_response",
                raw_length=len(raw_response),
                **log_ctx,
            )
        except Exception as exc:
            log.error("policy_gate.api_error", error=str(exc), **log_ctx)
            gate_input = _build_gate_input(
                model_version=self._model,
                template=template,
                template_vars=template_vars,
                corpus_version=corpus_version,
                rendered_prompt=rendered,
                prompt_snapshot=prompt_snapshot,
            )
            gate_output = _build_gate_output(
                verdict=None, raw_response="", token_cost=None
            )
            return GateResult(
                gate_input=gate_input,
                gate_output=gate_output,
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            )

        verdict = _parse_verdict(raw_response, decision_id=decision_id)
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)

        if verdict is None:
            log.warning("policy_gate.output_invalid", **log_ctx)
        else:
            log.info(
                "policy_gate.evaluated",
                permitted_actions=[a.value for a in verdict.permitted_actions],
                confidence=verdict.confidence,
                **log_ctx,
            )

        gate_input = _build_gate_input(
            model_version=self._model,
            template=template,
            template_vars=template_vars,
            corpus_version=corpus_version,
            rendered_prompt=rendered,
            prompt_snapshot=prompt_snapshot,
        )
        gate_output = _build_gate_output(
            verdict=verdict,
            raw_response=raw_response,
            token_cost=token_cost_obj,
        )
        return GateResult(
            gate_input=gate_input,
            gate_output=gate_output,
            latency_ms=latency_ms,
        )
