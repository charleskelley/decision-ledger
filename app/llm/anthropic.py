"""``LLMClient`` adapter backed by Anthropic's async SDK.

Anthropic doesn't expose a native ``response_format`` parameter for
strict JSON output. The accepted pattern is **tool use**: define a
single tool whose ``input_schema`` is the Pydantic model's JSON Schema,
force the model to call it via ``tool_choice``, and parse the
resulting ``tool_use`` block as the structured response.

This module is the only place under ``app/`` permitted to import the
Anthropic SDK.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, TypeVar, cast

from pydantic import BaseModel

from app.llm._pricing import compute_cost_usd
from core.llm.client import CompletionResult, TokenUsage

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

T = TypeVar("T", bound=BaseModel)

_TOOL_NAME = "structured_response"


class AnthropicLLMClient:
    """``LLMClient`` implementation backed by ``AsyncAnthropic``.

    Args:
        client: Authenticated ``AsyncAnthropic`` instance.
        model: Concrete model id (e.g., ``"claude-sonnet-4-6"``).
        timeout_secs: Per-request timeout forwarded to the SDK.
        max_tokens: Maximum output tokens — Anthropic requires this
            on every call (no implicit default like OpenAI).
    """

    def __init__(
        self,
        *,
        client: AsyncAnthropic,
        model: str = "claude-sonnet-4-6",
        timeout_secs: float = 30.0,
        max_tokens: int = 4096,
    ) -> None:
        """Initialize with an AsyncAnthropic client and model id."""
        self._client = client
        self._model = model
        self._timeout = timeout_secs
        self._max_tokens = max_tokens

    @property
    def model(self) -> str:
        """Return the model id this client was configured with."""
        return self._model

    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        response_format: type[T],
    ) -> CompletionResult[T]:
        """Call Anthropic via forced tool use and wrap the parsed result.

        Raises:
            RuntimeError: Response contained no ``tool_use`` block (the
                model declined to call the forced tool, or returned an
                unexpected content shape). Re-raised SDK-neutral.
        """
        schema = response_format.model_json_schema()
        tool = {
            "name": _TOOL_NAME,
            "description": (
                "Return the structured response. You MUST call this tool "
                "with arguments matching the schema."
            ),
            "input_schema": schema,
        }

        t0 = time.perf_counter()
        # SDK uses TypedDict for tools; our dict matches at runtime but
        # not the strict union type (ToolUnionParam).
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[tool],  # type: ignore[list-item]
            tool_choice={"type": "tool", "name": _TOOL_NAME},
            timeout=self._timeout,
        )
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)

        tool_input: dict | None = None
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                tool_input = cast("dict", getattr(block, "input", None))
                break
        if tool_input is None:
            msg = (
                "Anthropic returned no tool_use block; cannot extract "
                "structured response."
            )
            raise RuntimeError(msg)

        parsed = response_format.model_validate(tool_input)

        anthropic_usage = response.usage
        prompt_tokens = getattr(anthropic_usage, "input_tokens", 0)
        completion_tokens = getattr(anthropic_usage, "output_tokens", 0)
        total_tokens = prompt_tokens + completion_tokens
        usage = TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            model=self._model,
            cost_usd=compute_cost_usd(
                model=self._model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            ),
        )
        return CompletionResult(
            parsed=parsed,
            usage=usage,
            latency_ms=latency_ms,
        )
