"""Policy gate orchestrator — prompt rendering, LLM invocation, output validation.

PolicyGate renders the prompt template, calls the OpenAI API, validates the
structured JSON response against PolicyGateOutput, and returns a GateResult
with all fields needed to populate the gate-layer columns of DecisionBundle.

Any failure (template render error, API error, JSON parse failure, schema
validation failure) produces a GateResult with policy_gate_output=None.
The enforcement layer handles that via Tier 1 (schema failure → HOLD).

Usage::

    from openai import OpenAI
    from app.policy_gate.gate import PolicyGate
    from app.policy_gate.prompt_registry import YamlPromptRegistry

    gate = PolicyGate(OpenAI(), YamlPromptRegistry())
    result = gate.evaluate(obs, snippets, decision_id="dec-abc123")
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from pydantic import ValidationError

from core.bundle import TokenCost
from core.gate import PolicyGateOutput, PromptSnapshot

if TYPE_CHECKING:
    from openai import OpenAI

    from core.bundle import PolicySnippet
    from core.gate import PromptRegistry
    from core.observation import Observation

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

    Carries all fields required to populate the gate-layer columns of
    DecisionBundle. If any stage failed, policy_gate_output is None and
    enforcement routes to HOLD via Tier 1.

    Args:
        rendered_prompt: Full prompt sent to the LLM (template vars and
            policy snippets already substituted).
        raw_llm_response: Raw string content returned by the LLM API.
            Empty string if the API call did not complete.
        policy_gate_output: Validated structured output, or None when the
            LLM response could not be parsed or failed schema validation.
        token_cost: Token usage and cost breakdown; None if the API call
            did not complete.
        prompt_snapshot: Point-in-time snapshot of the prompt template at
            invocation time. None when the template could not be resolved.
        model_version: LLM model identifier used for this evaluation.
        latency_ms: Wall-clock time for the full evaluate() call in ms.
    """

    rendered_prompt: str
    raw_llm_response: str
    policy_gate_output: PolicyGateOutput | None
    token_cost: TokenCost | None
    prompt_snapshot: PromptSnapshot | None
    model_version: str
    latency_ms: float


# ---------------------------------------------------------------------------
# Private helpers (pure — no I/O; testable without infrastructure)
# ---------------------------------------------------------------------------


def _render_snippets(snippets: list[PolicySnippet]) -> str:
    """Format retrieved policy snippets into prompt-ready text.

    Each snippet is numbered and rendered with source metadata followed by
    verbatim text. Numbered from 1 so the LLM can reference them by index
    in the citations it produces.

    Args:
        snippets: Policy chunks returned by the retriever.

    Returns:
        Multi-line string ready for ``{policy_snippets}`` substitution.
        Returns ``"(no policy evidence retrieved)"`` when the list is empty.
    """
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
    snippets: list[PolicySnippet],
) -> str:
    """Render a prompt template with domain variables and policy snippets.

    Injects policy snippets first (with brace-escaping to prevent
    ``str.format()`` from misinterpreting any ``{ }`` characters in snippet
    text as placeholders), then substitutes all domain ``template_vars``.

    Args:
        template_str: Raw template content from ``PromptTemplate.template_str``.
        template_vars: Domain-provided variable values from
            ``GateContext.template_vars``.
        snippets: Policy chunks to inject at ``{policy_snippets}``.

    Returns:
        Fully rendered prompt string.

    Raises:
        KeyError: If any ``{placeholder}`` in ``template_str`` is absent from
            ``template_vars`` — indicates a mismatch in the domain assembler.
    """
    snippets_text = _render_snippets(snippets)
    # Escape braces in snippet text so str.format() does not treat { } in
    # policy document content as format placeholders.
    snippets_escaped = snippets_text.replace("{", "{{").replace("}", "}}")
    interim = template_str.replace("{policy_snippets}", snippets_escaped)
    return interim.format(**template_vars)


def _parse_gate_output(
    raw_response: str,
    *,
    decision_id: str,
) -> PolicyGateOutput | None:
    """Parse and validate a raw LLM response string as PolicyGateOutput.

    Returns None on any parse or validation error without raising. The caller
    logs the raw response; enforcement routes to HOLD via Tier 1.

    Uses ``strict=False`` in ``model_validate`` because LLM output is external
    JSON (plain strings and numbers) that must be coerced to internal types
    such as ``DecisionAction`` StrEnum members.

    Args:
        raw_response: Raw string content from the LLM completion.
        decision_id: Decision ID for structured log context.

    Returns:
        Validated ``PolicyGateOutput``, or ``None`` on any failure.
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
        # strict=False: coerce raw JSON types (e.g. "ALLOW" str → DecisionAction)
        return PolicyGateOutput.model_validate(data, strict=False)
    except ValidationError as exc:
        log.warning(
            "policy_gate.schema_validation_error",
            component="policy_gate",
            decision_id=decision_id,
            error_count=exc.error_count(),
        )
        return None


def _compute_token_cost(usage: object, model: str) -> TokenCost:
    """Build a TokenCost from an OpenAI CompletionUsage object.

    Cost is estimated using approximate per-token pricing — not billing.

    Args:
        usage: OpenAI ``CompletionUsage`` with ``prompt_tokens``,
            ``completion_tokens``, and ``total_tokens``.
        model: Model identifier string.

    Returns:
        ``TokenCost`` with usage counts and estimated USD cost.
    """
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


# ---------------------------------------------------------------------------
# PolicyGate
# ---------------------------------------------------------------------------


class PolicyGate:
    """Policy gate orchestrator — prompt rendering, LLM call, output validation.

    Wraps the OpenAI API call in a structured evaluation loop. Any failure
    (network error, timeout, JSON parse failure, schema validation failure)
    produces a ``GateResult`` with ``policy_gate_output=None`` so enforcement
    can apply Tier 1 (schema failure → HOLD) deterministically.

    The gate is stateless — it holds no per-request state. Construct once at
    process startup and reuse across requests (the underlying OpenAI client
    handles connection pooling).

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
        snippets: list[PolicySnippet],
        *,
        decision_id: str,
    ) -> GateResult:
        """Evaluate an observation against retrieved policy evidence.

        Renders the prompt template, calls the LLM, validates the response
        as ``PolicyGateOutput``, and returns a ``GateResult`` with all fields
        needed to assemble the gate-layer columns of ``DecisionBundle``.

        Any error (render failure, API error, JSON parse failure, schema
        validation failure) is logged and results in a ``GateResult`` with
        ``policy_gate_output=None``. Enforcement handles this via Tier 1.

        Args:
            obs: Assembled observation with ``gate_context`` populated.
            snippets: Policy chunks from the retriever for this observation.
            decision_id: Decision ID for log context and audit tracing.

        Returns:
            ``GateResult`` with all gate-layer fields populated.
        """
        t0 = time.perf_counter()
        log_ctx = {
            "component": "policy_gate",
            "decision_id": decision_id,
            "event_id": obs.event_id,
            "model": self._model,
        }

        # --- Render prompt ---
        template = self._registry.get(obs.gate_context.prompt_template_id)
        prompt_snapshot: PromptSnapshot = PromptSnapshot(
            template_id=template.template_id,
            version=template.version,
            template_str=template.template_str,
        )
        try:
            rendered = _render_prompt(
                template.template_str,
                obs.gate_context.template_vars,
                snippets,
            )
        except KeyError as exc:
            log.error("policy_gate.render_error", error=str(exc), **log_ctx)
            return GateResult(
                rendered_prompt="",
                raw_llm_response="",
                policy_gate_output=None,
                token_cost=None,
                prompt_snapshot=prompt_snapshot,
                model_version=self._model,
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            )

        # --- Call LLM ---
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
            return GateResult(
                rendered_prompt=rendered,
                raw_llm_response="",
                policy_gate_output=None,
                token_cost=None,
                prompt_snapshot=prompt_snapshot,
                model_version=self._model,
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            )

        # --- Parse and validate ---
        gate_output = _parse_gate_output(raw_response, decision_id=decision_id)
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)

        if gate_output is None:
            log.warning("policy_gate.output_invalid", **log_ctx)
        else:
            log.info(
                "policy_gate.evaluated",
                permitted_actions=[a.value for a in gate_output.permitted_actions],
                confidence=gate_output.confidence,
                **log_ctx,
            )

        return GateResult(
            rendered_prompt=rendered,
            raw_llm_response=raw_response,
            policy_gate_output=gate_output,
            token_cost=token_cost_obj,
            prompt_snapshot=prompt_snapshot,
            model_version=self._model,
            latency_ms=latency_ms,
        )
