"""Contract tests for ``core/llm/client.py``.

Verifies the universal LLM-call primitive's shape: ``TokenUsage``
validation, ``CompletionResult[T]`` generic wrapping, and ``LLMClient``
Protocol structural conformance.

Concrete adapters are tested in ``tests/app/llm/`` against their
respective SDKs.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, ValidationError

from core.llm import CompletionResult, LLMClient, TokenUsage

# ---------------------------------------------------------------------------
# TokenUsage contract
# ---------------------------------------------------------------------------


def test_token_usage_accepts_positive_counts():
    usage = TokenUsage(
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        model="gpt-4o-mini",
        cost_usd=0.001,
    )
    assert usage.prompt_tokens == 100
    assert usage.cost_usd == 0.001


def test_token_usage_allows_none_cost():
    """Unknown model id → cost_usd=None is the documented contract."""
    usage = TokenUsage(
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        model="some-future-model",
        cost_usd=None,
    )
    assert usage.cost_usd is None


def test_token_usage_rejects_negative_counts():
    with pytest.raises(ValidationError):
        TokenUsage(
            prompt_tokens=-1,
            completion_tokens=0,
            total_tokens=0,
            model="gpt-4o-mini",
        )


def test_token_usage_rejects_negative_cost():
    with pytest.raises(ValidationError):
        TokenUsage(
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            model="gpt-4o-mini",
            cost_usd=-0.01,
        )


def test_token_usage_rejects_empty_model_id():
    with pytest.raises(ValidationError):
        TokenUsage(
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            model="",
        )


def test_token_usage_is_immutable():
    usage = TokenUsage(
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        model="gpt-4o-mini",
    )
    with pytest.raises(ValidationError):
        usage.model = "different-model"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CompletionResult contract
# ---------------------------------------------------------------------------


class _DummyResponse(BaseModel):
    answer: str


def test_completion_result_wraps_parsed_with_metadata():
    result = CompletionResult[_DummyResponse](
        parsed=_DummyResponse(answer="hello"),
        usage=TokenUsage(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            model="gpt-4o-mini",
            cost_usd=0.0005,
        ),
        latency_ms=42.0,
    )
    assert result.parsed.answer == "hello"
    assert result.usage.prompt_tokens == 10
    assert result.latency_ms == 42.0


def test_completion_result_rejects_negative_latency():
    with pytest.raises(ValidationError):
        CompletionResult[_DummyResponse](
            parsed=_DummyResponse(answer="hello"),
            usage=TokenUsage(
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
                model="gpt-4o-mini",
            ),
            latency_ms=-1.0,
        )


# ---------------------------------------------------------------------------
# LLMClient protocol conformance
# ---------------------------------------------------------------------------


class _ConformantClient:
    """Minimal class that structurally satisfies the LLMClient protocol."""

    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        response_format: type,
    ) -> CompletionResult:
        return CompletionResult(
            parsed=response_format(answer=f"echo: {user}"),
            usage=TokenUsage(
                prompt_tokens=len(system) + len(user),
                completion_tokens=10,
                total_tokens=len(system) + len(user) + 10,
                model="stub",
            ),
            latency_ms=0.1,
        )


def test_conformant_class_satisfies_llm_client_protocol():
    """Type-narrowing: a duck-typed conforming class IS an LLMClient."""
    client: LLMClient = _ConformantClient()
    result = asyncio.run(
        client.complete_structured(
            system="role",
            user="task",
            response_format=_DummyResponse,
        )
    )
    assert isinstance(result, CompletionResult)
    assert result.parsed.answer == "echo: task"
    assert result.usage.model == "stub"
