"""``LLMClient`` adapter backed by OpenAI's async SDK.

Uses ``AsyncOpenAI.beta.chat.completions.parse`` so the SDK enforces
JSON Schema conformance against the supplied Pydantic model and returns
a validated instance. This is the strict-structured-output path; legacy
``response_format={"type": "json_object"}`` (loose JSON mode) is not
used.

This module is the only place in the project permitted to import the
OpenAI SDK. Callers construct ``OpenAILLMClient()`` without needing to
import ``AsyncOpenAI`` themselves — the adapter default-constructs the
SDK client (which reads ``OPENAI_API_KEY`` from the environment).
"""

from __future__ import annotations

import time
from typing import TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.llm._pricing import compute_cost_usd
from core.llm.client import CompletionResult, TokenUsage

T = TypeVar("T", bound=BaseModel)


class OpenAILLMClient:
    """``LLMClient`` implementation backed by ``AsyncOpenAI``.

    Args:
        client: Optional ``AsyncOpenAI`` instance. Defaults to a
            fresh ``AsyncOpenAI()`` reading auth from the environment
            (``OPENAI_API_KEY``). Tests override with a mocked client.
        model: Concrete model id (e.g., ``"gpt-4o-2024-08-06"``).
        timeout_secs: Per-request timeout forwarded to the SDK.
    """

    def __init__(
        self,
        *,
        client: AsyncOpenAI | None = None,
        model: str = "gpt-4o-2024-08-06",
        timeout_secs: float = 30.0,
    ) -> None:
        """Initialize with an optional AsyncOpenAI client and model id."""
        self._client = client if client is not None else AsyncOpenAI()
        self._model = model
        self._timeout = timeout_secs

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
        """Call OpenAI structured outputs and wrap the result.

        Raises:
            RuntimeError: SDK returned no parsed output (refusal,
                schema-validation failure, etc.). Re-raised SDK-neutral.
        """
        t0 = time.perf_counter()
        response = await self._client.beta.chat.completions.parse(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format=response_format,
            timeout=self._timeout,
        )
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        parsed = response.choices[0].message.parsed
        if parsed is None:
            msg = (
                "OpenAI returned no parsed structured output "
                "(refusal or schema-validation failure)."
            )
            raise RuntimeError(msg)

        usage_obj = response.usage
        prompt_tokens = usage_obj.prompt_tokens if usage_obj else 0
        completion_tokens = usage_obj.completion_tokens if usage_obj else 0
        total_tokens = usage_obj.total_tokens if usage_obj else 0
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
