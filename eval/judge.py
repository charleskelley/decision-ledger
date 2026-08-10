"""LLM-as-judge primitive for the eval harness.

The judge consumes the universal ``core.llm.LLMClient`` (DR-23) — same
adapter the policy gate uses. Swapping models means swapping the
``LLMClient`` instance at the wiring edge; nothing inside ``eval/judge.py``,
``eval/dimensions/``, or ``eval/runners/`` changes.

Judge-domain types (``JudgeOutput``, ``JudgePromptRegistry``,
``JudgePromptTemplate``) live here. They wrap the universal LLM call
with the judge's score-and-reasoning rubric.

Usage::

    from openai import AsyncOpenAI
    from app.llm.openai import OpenAILLMClient
    from eval.judge import JudgePromptRegistry, llm_judge

    registry = JudgePromptRegistry()
    template = registry.get("faithfulness_grounding")
    client = OpenAILLMClient(client=AsyncOpenAI(), model="gpt-4o-2024-08-06")
    output = await llm_judge(
        template=template,
        template_vars={"rationale": "...", "context": "..."},
        client=client,
    )
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from core.llm import LLMClient

_PROMPTS_DIR = Path(__file__).parent / "prompts" / "judges"

# Variable placeholder regex — same convention as app/gate/policy/prompt_registry.py.
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


class JudgeOutput(BaseModel):
    """Structured output schema produced by every judge invocation.

    Single uniform shape across faithfulness, citation, and any future judge:
    a ``[0, 1]`` score plus a free-form reasoning string. Dimensions interpret
    the score per their own rubric (e.g., the citation_relevance prompt asks
    the judge to map a 1-5 rating into [0, 1]).

    Attributes:
        score: Normalized score in ``[0, 1]``.
        reasoning: Free-form rationale for the score.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    score: float = Field(ge=0.0, le=1.0)
    reasoning: str


@dataclass(frozen=True)
class JudgePromptTemplate:
    """Loaded judge prompt template.

    Args:
        template_id: Stable identifier (matches YAML ``template_id`` field;
            falls back to filename stem).
        version: Semantic version string from the YAML.
        description: One-line description for human readers.
        system_prompt: System message setting the judge's role/rubric.
        user_template: User message with ``{var}`` placeholders the dimension
            fills in at call time.
        required_vars: Variable names the dimension must supply.
    """

    template_id: str
    version: str
    description: str
    system_prompt: str
    user_template: str
    required_vars: frozenset[str]


def _load_template(path: Path) -> JudgePromptTemplate:
    """Parse one YAML judge file into a ``JudgePromptTemplate``.

    Args:
        path: Path to the ``.yaml`` file.

    Returns:
        Parsed ``JudgePromptTemplate``.

    Raises:
        KeyError: If the YAML is missing a required field.
        ValueError: If the system or user template content is empty.
    """
    raw: dict = yaml.safe_load(path.read_text(encoding="utf-8"))
    system: str = raw["system"]
    user: str = raw["user"]
    if not system.strip():
        raise ValueError(f"Judge template '{path.name}' has empty system prompt.")
    if not user.strip():
        raise ValueError(f"Judge template '{path.name}' has empty user template.")
    required = frozenset(_PLACEHOLDER_RE.findall(user))
    return JudgePromptTemplate(
        template_id=raw.get("template_id", path.stem),
        version=raw["version"],
        description=raw["description"],
        system_prompt=system,
        user_template=user,
        required_vars=required,
    )


class JudgePromptRegistry:
    """File-based registry for judge prompt templates.

    Loads all ``*.yaml`` files in ``eval/prompts/judges/`` at construction.
    Templates are immutable once loaded — same pattern as the policy gate's
    ``YamlPromptRegistry``.

    Args:
        prompts_dir: Directory to scan. Override in tests with a fixture dir.

    Raises:
        ValueError: If any YAML fails to parse or has empty content.
    """

    def __init__(self, prompts_dir: Path = _PROMPTS_DIR) -> None:
        """Load all *.yaml files from ``prompts_dir`` into the in-memory registry."""
        self._templates: dict[str, JudgePromptTemplate] = {}
        for path in sorted(prompts_dir.glob("*.yaml")):
            tmpl = _load_template(path)
            self._templates[tmpl.template_id] = tmpl

    def get(self, template_id: str) -> JudgePromptTemplate:
        """Retrieve a template by ID.

        Args:
            template_id: Stable identifier.

        Returns:
            The registered template.

        Raises:
            KeyError: If the ID is not registered.
        """
        try:
            return self._templates[template_id]
        except KeyError:
            registered = sorted(self._templates)
            raise KeyError(
                f"Judge template '{template_id}' not found. Registered: {registered}"
            ) from None

    def registered_ids(self) -> list[str]:
        """Return a sorted list of registered template IDs."""
        return sorted(self._templates)


def _render_user(template: JudgePromptTemplate, template_vars: dict[str, str]) -> str:
    """Render the user template with the provided variables.

    Raises:
        ValueError: If any required variable is missing.
        KeyError: If the template references an unprovided placeholder.
    """
    missing = template.required_vars - template_vars.keys()
    if missing:
        raise ValueError(
            f"Judge template '{template.template_id}' requires variables "
            f"{sorted(missing)} but they were not provided. "
            f"Provided: {sorted(template_vars)}"
        )
    return template.user_template.format(**template_vars)


async def llm_judge(
    *,
    template: JudgePromptTemplate,
    template_vars: dict[str, str],
    client: LLMClient,
) -> JudgeOutput:
    """Run a judge invocation against the provided LLM client.

    The client is opaque — judges work the same against OpenAI, Anthropic,
    or any other backing model as long as the adapter satisfies the
    ``LLMClient`` protocol.

    Args:
        template: The judge prompt template to use.
        template_vars: Variable values to fill the user template.
        client: SDK-agnostic ``LLMClient`` implementation.

    Returns:
        Validated ``JudgeOutput`` from the LLM. The underlying
        ``CompletionResult.usage`` is discarded here — judge-side cost
        tracking can pull it directly from the client when needed.

    Raises:
        ValueError: If required template variables are missing.
    """
    user = _render_user(template, template_vars)
    result = await client.complete_structured(
        system=template.system_prompt,
        user=user,
        response_format=JudgeOutput,
    )
    return result.parsed
