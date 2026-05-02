"""Tests for ``app/llm/anthropic.py:AnthropicLLMClient``.

Adapter behavior verified against a mocked ``AsyncAnthropic`` — no
network calls, no API key required.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from app.llm.anthropic import AnthropicLLMClient


class _Reply(BaseModel):
    answer: str


def _mock_anthropic_response(
    *,
    tool_input: dict | None,
    input_tokens: int = 10,
    output_tokens: int = 5,
):
    """Build a MagicMock matching the Anthropic messages.create response shape."""
    blocks = []
    if tool_input is not None:
        blocks.append(SimpleNamespace(type="tool_use", input=tool_input))
    return SimpleNamespace(
        content=blocks,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ),
    )


def _mock_async_client(*, tool_input: dict | None, **kwargs) -> MagicMock:
    client = MagicMock()
    client.messages.create = AsyncMock(
        return_value=_mock_anthropic_response(tool_input=tool_input, **kwargs)
    )
    return client


def test_complete_structured_returns_parsed_result():
    """Tool input is validated through the response_format Pydantic model."""
    raw_client = _mock_async_client(tool_input={"answer": "42"})
    client = AnthropicLLMClient(client=raw_client, model="claude-sonnet-4-6")

    result = asyncio.run(
        client.complete_structured(
            system="be terse", user="answer?", response_format=_Reply
        )
    )

    assert result.parsed.answer == "42"


def test_complete_structured_populates_token_usage():
    """Anthropic input_tokens/output_tokens map onto the neutral primitive."""
    raw_client = _mock_async_client(
        tool_input={"answer": "ok"}, input_tokens=200, output_tokens=30
    )
    client = AnthropicLLMClient(client=raw_client, model="claude-sonnet-4-6")

    result = asyncio.run(
        client.complete_structured(system="role", user="task", response_format=_Reply)
    )

    assert result.usage.prompt_tokens == 200
    assert result.usage.completion_tokens == 30
    assert result.usage.total_tokens == 230
    assert result.usage.model == "claude-sonnet-4-6"


def test_complete_structured_computes_cost_for_known_model():
    """Known model id → cost_usd populated from the pricing table."""
    raw_client = _mock_async_client(
        tool_input={"answer": "ok"}, input_tokens=1_000_000, output_tokens=0
    )
    client = AnthropicLLMClient(client=raw_client, model="claude-sonnet-4-6")

    result = asyncio.run(
        client.complete_structured(system="role", user="task", response_format=_Reply)
    )

    # claude-sonnet-4-6 input = $3.00/M; 1M prompt tokens → $3.00
    assert result.usage.cost_usd == pytest.approx(3.00)


def test_complete_structured_returns_none_cost_for_unknown_model():
    """Unknown model id → cost_usd is None."""
    raw_client = _mock_async_client(tool_input={"answer": "ok"})
    client = AnthropicLLMClient(client=raw_client, model="some-future-claude-model")

    result = asyncio.run(
        client.complete_structured(system="role", user="task", response_format=_Reply)
    )

    assert result.usage.cost_usd is None


def test_complete_structured_raises_runtime_error_on_no_tool_use():
    """No tool_use block in the response → RuntimeError (model declined)."""
    raw_client = _mock_async_client(tool_input=None)
    client = AnthropicLLMClient(client=raw_client, model="claude-sonnet-4-6")

    with pytest.raises(RuntimeError, match="no tool_use block"):
        asyncio.run(
            client.complete_structured(
                system="role", user="task", response_format=_Reply
            )
        )


def test_forces_tool_choice_to_structured_response_tool():
    """Adapter sends tool_choice forcing the structured tool call."""
    raw_client = _mock_async_client(tool_input={"answer": "ok"})
    client = AnthropicLLMClient(client=raw_client, model="claude-sonnet-4-6")

    asyncio.run(
        client.complete_structured(system="role", user="task", response_format=_Reply)
    )

    call_kwargs = raw_client.messages.create.call_args.kwargs
    assert call_kwargs["tool_choice"] == {
        "type": "tool",
        "name": "structured_response",
    }
    assert len(call_kwargs["tools"]) == 1
    assert call_kwargs["tools"][0]["name"] == "structured_response"
    assert call_kwargs["tools"][0]["input_schema"] == _Reply.model_json_schema()


def test_passes_system_and_user_separately():
    """system goes to the `system` arg; user goes into messages."""
    raw_client = _mock_async_client(tool_input={"answer": "ok"})
    client = AnthropicLLMClient(client=raw_client, model="claude-sonnet-4-6")

    asyncio.run(
        client.complete_structured(
            system="you are a judge",
            user="grade this",
            response_format=_Reply,
        )
    )

    call_kwargs = raw_client.messages.create.call_args.kwargs
    assert call_kwargs["system"] == "you are a judge"
    assert call_kwargs["messages"] == [{"role": "user", "content": "grade this"}]


def test_model_property_exposes_configured_id():
    client = AnthropicLLMClient(client=MagicMock(), model="claude-haiku-4-5-20251001")
    assert client.model == "claude-haiku-4-5-20251001"
