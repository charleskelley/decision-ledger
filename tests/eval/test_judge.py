"""Unit tests for the SDK-agnostic judge primitive.

These tests intentionally do not invoke any LLM. The judge protocol abstracts
the SDK away; the live OpenAI integration is exercised by the eval harness
under ``@pytest.mark.evaluation``. If anything in this file needs to import
``openai``, the abstraction has leaked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

import pytest
from pydantic import BaseModel, ValidationError

from eval.judge import (
    JudgeClient,
    JudgeOutput,
    JudgePromptRegistry,
    JudgePromptTemplate,
    _render_user,
    llm_judge,
)

if TYPE_CHECKING:
    from pathlib import Path

T = TypeVar("T", bound=BaseModel)


# ---------------------------------------------------------------------------
# JudgeOutput schema
# ---------------------------------------------------------------------------


class TestJudgeOutput:
    def test_valid_score_in_range(self) -> None:
        out = JudgeOutput(score=0.5, reasoning="halfway")
        assert out.score == 0.5
        assert out.reasoning == "halfway"

    def test_score_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            JudgeOutput(score=1.5, reasoning="over")

    def test_negative_score_rejected(self) -> None:
        with pytest.raises(ValidationError):
            JudgeOutput(score=-0.1, reasoning="under")

    def test_frozen(self) -> None:
        out = JudgeOutput(score=0.5, reasoning="x")
        with pytest.raises(ValidationError):
            out.score = 0.7


# ---------------------------------------------------------------------------
# JudgePromptRegistry
# ---------------------------------------------------------------------------


class TestJudgePromptRegistry:
    def test_loads_default_prompts(self) -> None:
        registry = JudgePromptRegistry()
        ids = registry.registered_ids()
        assert "faithfulness_grounding" in ids
        assert "citation_relevance" in ids
        assert "citation_entailment" in ids

    def test_get_returns_template(self) -> None:
        registry = JudgePromptRegistry()
        tmpl = registry.get("faithfulness_grounding")
        assert tmpl.template_id == "faithfulness_grounding"
        assert tmpl.version == "1.0.0"
        assert "{rationale}" in tmpl.user_template
        assert "{context}" in tmpl.user_template
        assert tmpl.required_vars == frozenset({"rationale", "context"})

    def test_get_unknown_raises(self) -> None:
        registry = JudgePromptRegistry()
        with pytest.raises(KeyError, match="not found"):
            registry.get("nonexistent_judge")

    def test_custom_prompts_dir(self, tmp_path: Path) -> None:
        # Write a fixture YAML and confirm the registry loads it.
        fixture = tmp_path / "stub_judge.yaml"
        fixture.write_text(
            "template_id: stub_judge\n"
            "version: 0.0.1\n"
            "description: stub\n"
            "system: 'sys'\n"
            "user: 'do {thing}'\n",
            encoding="utf-8",
        )
        registry = JudgePromptRegistry(prompts_dir=tmp_path)
        tmpl = registry.get("stub_judge")
        assert tmpl.required_vars == frozenset({"thing"})

    def test_empty_system_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "template_id: bad\nversion: 0.0.1\ndescription: x\n"
            "system: '   '\nuser: 'foo'\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="empty system prompt"):
            JudgePromptRegistry(prompts_dir=tmp_path)


# ---------------------------------------------------------------------------
# _render_user
# ---------------------------------------------------------------------------


class TestRenderUser:
    @staticmethod
    def _tmpl(user: str, required: set[str]) -> JudgePromptTemplate:
        return JudgePromptTemplate(
            template_id="t",
            version="0.0.1",
            description="d",
            system_prompt="s",
            user_template=user,
            required_vars=frozenset(required),
        )

    def test_fills_variables(self) -> None:
        tmpl = self._tmpl("Hello {name}!", {"name"})
        rendered = _render_user(tmpl, {"name": "Charles"})
        assert rendered == "Hello Charles!"

    def test_missing_variable_raises(self) -> None:
        tmpl = self._tmpl("Hello {name}", {"name"})
        with pytest.raises(ValueError, match="requires variables"):
            _render_user(tmpl, {})

    def test_extra_variables_ignored(self) -> None:
        tmpl = self._tmpl("Hello {name}", {"name"})
        rendered = _render_user(tmpl, {"name": "X", "unused": "y"})
        assert rendered == "Hello X"


# ---------------------------------------------------------------------------
# llm_judge — exercises a stub JudgeClient (SDK-agnostic boundary check)
# ---------------------------------------------------------------------------


class _StubJudgeClient:
    """Records inputs; returns a canned JudgeOutput.

    If anything inside ``llm_judge`` (or future eval/dimensions) requires the
    client to be a specific SDK class, this stub will fail to drive it — that
    is the SDK-agnostic boundary check.
    """

    def __init__(self, *, score: float = 0.7, reasoning: str = "stub") -> None:
        self.calls: list[dict] = []
        self._score = score
        self._reasoning = reasoning

    async def complete_structured(
        self,
        *,
        system: str,
        user: str,
        response_format: type[T],
    ) -> T:
        self.calls.append(
            {"system": system, "user": user, "response_format": response_format}
        )
        # Build an instance of the requested response_format. JudgeOutput is
        # the only schema currently used; for any other type the stub
        # delegates to model_validate with the canned payload.
        payload = {"score": self._score, "reasoning": self._reasoning}
        return response_format.model_validate(payload)


class TestLlmJudge:
    def test_stub_satisfies_protocol(self) -> None:
        # Static-typing assertion via runtime structural check — if eval ever
        # depends on a concrete SDK type the stub will stop satisfying this.
        stub: JudgeClient = _StubJudgeClient()
        assert callable(stub.complete_structured)

    @pytest.mark.asyncio
    async def test_renders_prompt_and_invokes_client(self) -> None:
        tmpl = JudgePromptTemplate(
            template_id="t",
            version="0.0.1",
            description="d",
            system_prompt="judge sys",
            user_template="claim={claim} evidence={evidence}",
            required_vars=frozenset({"claim", "evidence"}),
        )
        stub = _StubJudgeClient(score=0.42, reasoning="ok")
        out = await llm_judge(
            template=tmpl,
            template_vars={"claim": "C", "evidence": "E"},
            client=stub,
        )
        assert out.score == 0.42
        assert out.reasoning == "ok"
        assert len(stub.calls) == 1
        call = stub.calls[0]
        assert call["system"] == "judge sys"
        assert call["user"] == "claim=C evidence=E"
        assert call["response_format"] is JudgeOutput

    @pytest.mark.asyncio
    async def test_missing_var_raises_before_call(self) -> None:
        tmpl = JudgePromptTemplate(
            template_id="t",
            version="0.0.1",
            description="d",
            system_prompt="s",
            user_template="hello {name}",
            required_vars=frozenset({"name"}),
        )
        stub = _StubJudgeClient()
        with pytest.raises(ValueError, match="requires variables"):
            await llm_judge(template=tmpl, template_vars={}, client=stub)
        assert stub.calls == []  # no call made
