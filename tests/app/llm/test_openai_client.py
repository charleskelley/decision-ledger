"""Tests for ``app/llm/openai.py:OpenAILLMClient``.

Adapter behavior verified against a mocked ``AsyncOpenAI`` — no network
calls, no API key required. Live behavior is exercised by the Step 5
scenario smoke test.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from app.llm.openai import OpenAILLMClient


class _Reply(BaseModel):
    answer: str


def _mock_openai_response(parsed: _Reply, *, prompt_t: int = 10, completion_t: int = 5):
    """Build a MagicMock matching the AsyncOpenAI structured-parse response shape."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.parsed = parsed
    response.usage.prompt_tokens = prompt_t
    response.usage.completion_tokens = completion_t
    response.usage.total_tokens = prompt_t + completion_t
    return response


def _mock_async_client(parsed: _Reply, **kwargs) -> MagicMock:
    client = MagicMock()
    client.beta.chat.completions.parse = AsyncMock(
        return_value=_mock_openai_response(parsed, **kwargs)
    )
    return client


def test_complete_structured_returns_parsed_result():
    """Adapter wraps the SDK's parsed instance into a CompletionResult."""
    expected = _Reply(answer="42")
    client = OpenAILLMClient(client=_mock_async_client(expected), model="gpt-4o-mini")

    result = asyncio.run(
        client.complete_structured(
            system="be terse",
            user="what is the answer?",
            response_format=_Reply,
        )
    )

    assert result.parsed.answer == "42"


def test_complete_structured_populates_token_usage():
    """Adapter copies prompt/completion/total tokens from the SDK response."""
    client = OpenAILLMClient(
        client=_mock_async_client(_Reply(answer="ok"), prompt_t=100, completion_t=20),
        model="gpt-4o-mini",
    )

    result = asyncio.run(
        client.complete_structured(
            system="role",
            user="task",
            response_format=_Reply,
        )
    )

    assert result.usage.prompt_tokens == 100
    assert result.usage.completion_tokens == 20
    assert result.usage.total_tokens == 120
    assert result.usage.model == "gpt-4o-mini"


def test_complete_structured_computes_cost_for_known_model():
    """Known model id → cost_usd populated from the pricing table."""
    raw_client = _mock_async_client(
        _Reply(answer="ok"), prompt_t=1_000_000, completion_t=0
    )
    client = OpenAILLMClient(client=raw_client, model="gpt-4o-mini")

    result = asyncio.run(
        client.complete_structured(system="role", user="task", response_format=_Reply)
    )

    # gpt-4o-mini input = $0.15/M; 1M prompt tokens, 0 output → $0.15
    assert result.usage.cost_usd == pytest.approx(0.15)


def test_complete_structured_returns_none_cost_for_unknown_model():
    """Unknown model id → cost_usd is None (not raised)."""
    client = OpenAILLMClient(
        client=_mock_async_client(_Reply(answer="ok")),
        model="some-future-openai-model",
    )

    result = asyncio.run(
        client.complete_structured(system="role", user="task", response_format=_Reply)
    )

    assert result.usage.cost_usd is None


def test_complete_structured_records_latency():
    """latency_ms is populated and non-negative."""
    client = OpenAILLMClient(
        client=_mock_async_client(_Reply(answer="ok")), model="gpt-4o-mini"
    )

    result = asyncio.run(
        client.complete_structured(system="role", user="task", response_format=_Reply)
    )

    assert result.latency_ms >= 0.0


def test_complete_structured_raises_runtime_error_on_no_parse():
    """SDK returning parsed=None (refusal/schema failure) → RuntimeError."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.parsed = None
    response.usage.prompt_tokens = 0
    response.usage.completion_tokens = 0
    response.usage.total_tokens = 0
    raw_client = MagicMock()
    raw_client.beta.chat.completions.parse = AsyncMock(return_value=response)

    client = OpenAILLMClient(client=raw_client, model="gpt-4o-mini")

    with pytest.raises(RuntimeError, match="no parsed structured output"):
        asyncio.run(
            client.complete_structured(
                system="role", user="task", response_format=_Reply
            )
        )


def test_passes_system_and_user_messages_to_sdk():
    """Adapter forwards system + user as separate role messages."""
    raw_client = _mock_async_client(_Reply(answer="ok"))
    client = OpenAILLMClient(client=raw_client, model="gpt-4o-mini")

    asyncio.run(
        client.complete_structured(
            system="you are a judge",
            user="grade this output",
            response_format=_Reply,
        )
    )

    call_kwargs = raw_client.beta.chat.completions.parse.call_args.kwargs
    assert call_kwargs["messages"] == [
        {"role": "system", "content": "you are a judge"},
        {"role": "user", "content": "grade this output"},
    ]
    assert call_kwargs["response_format"] is _Reply


def test_model_property_exposes_configured_id():
    client = OpenAILLMClient(client=MagicMock(), model="gpt-4o")
    assert client.model == "gpt-4o"
