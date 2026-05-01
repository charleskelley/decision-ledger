"""``JudgeClient`` implementation backed by OpenAI's async SDK.

This is the only module under ``eval/`` permitted to import an OpenAI SDK
class. All other ``eval/*`` code depends on the ``JudgeClient`` protocol in
``eval.judge``, never on a concrete SDK type.

Uses ``AsyncOpenAI.beta.chat.completions.parse`` so the SDK enforces JSON
Schema conformance against the supplied Pydantic model and returns a
validated instance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from openai import AsyncOpenAI

T = TypeVar("T", bound=BaseModel)


class OpenAIJudgeClient:
    """``JudgeClient`` backed by ``AsyncOpenAI`` structured outputs.

    Args:
        client: Authenticated ``AsyncOpenAI`` instance.
        model: Model ID (e.g., ``"gpt-4o-2024-08-06"``).
        timeout_secs: Per-request timeout forwarded to the SDK.
    """

    def __init__(
        self,
        *,
        client: AsyncOpenAI,
        model: str = "gpt-4o-2024-08-06",
        timeout_secs: float = 30.0,
    ) -> None:
        """Initialize the client with an AsyncOpenAI instance and model id."""
        self._client = client
        self._model = model
        self._timeout = timeout_secs

    @property
    def model(self) -> str:
        """Return the model ID this client was configured with."""
        return self._model

    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        response_format: type[T],
    ) -> T:
        """Issue a structured-completion call and return the parsed result.

        Args:
            system: System message setting the model's role.
            user: User message containing the task.
            response_format: Pydantic model class the response must validate
                against.

        Returns:
            An instance of ``response_format`` populated from the parsed
            response.

        Raises:
            RuntimeError: If the SDK returns no parsed output (e.g., a
                refusal). Re-raised as a plain RuntimeError to keep the
                error surface SDK-neutral for callers.
        """
        response = await self._client.beta.chat.completions.parse(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format=response_format,
            timeout=self._timeout,
        )
        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise RuntimeError(
                "OpenAI returned no parsed structured output for judge call."
            )
        return parsed
