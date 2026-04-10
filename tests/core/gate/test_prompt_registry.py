"""Behavioral tests for YamlPromptRegistry and PromptTemplate contracts.

Tests are split into two groups:
- PromptTemplate contract tests: pure Pydantic model validation, no filesystem.
- YamlPromptRegistry tests: use the real prompts/ directory (ato-v1.yaml).
  These tests depend on app/policy_gate/prompts/ato-v1.yaml being present.
  They do not require Docker or any infrastructure.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.policy_gate.prompt_registry import YamlPromptRegistry, _extract_required_vars
from core.gate import PromptRegistry, PromptTemplate

_PROMPTS_DIR = Path(__file__).parents[3] / "app" / "policy_gate" / "prompts"


# ---------------------------------------------------------------------------
# _extract_required_vars (pure function)
# ---------------------------------------------------------------------------


def test_extract_required_vars_finds_placeholders():
    template = "Hello {name}, your score is {score}."
    assert _extract_required_vars(template) == frozenset({"name", "score"})


def test_extract_required_vars_excludes_policy_snippets():
    template = "Context: {risk_score}\n\nPolicy:\n{policy_snippets}"
    assert _extract_required_vars(template) == frozenset({"risk_score"})


def test_extract_required_vars_empty_template():
    assert _extract_required_vars("No placeholders here.") == frozenset()


def test_extract_required_vars_deduplicates_repeated_placeholder():
    template = "{risk_score} is above {threshold}. Score: {risk_score}"
    assert _extract_required_vars(template) == frozenset({"risk_score", "threshold"})


# ---------------------------------------------------------------------------
# PromptTemplate contract
# ---------------------------------------------------------------------------


def test_prompt_template_required_vars_is_frozenset():
    t = PromptTemplate(
        template_id="test-v1",
        version="1.0.0",
        description="Test template.",
        template_str="Hello {name}.",
        required_vars=frozenset({"name"}),
    )
    assert isinstance(t.required_vars, frozenset)
    assert "name" in t.required_vars


def test_prompt_template_is_immutable():
    t = PromptTemplate(
        template_id="test-v1",
        version="1.0.0",
        description="Test.",
        template_str="Hello {name}.",
        required_vars=frozenset({"name"}),
    )
    with pytest.raises(ValidationError):
        t.template_id = "modified"


# ---------------------------------------------------------------------------
# YamlPromptRegistry — ato-v1.yaml (real file)
# ---------------------------------------------------------------------------


@pytest.fixture
def registry():
    return YamlPromptRegistry(prompts_dir=_PROMPTS_DIR)


def test_registry_loads_ato_v1(registry):
    template = registry.get("ato-v1")
    assert template.template_id == "ato-v1"
    assert template.version == "1.0.0"


def test_registry_ato_v1_has_policy_snippets_placeholder(registry):
    template = registry.get("ato-v1")
    assert "{policy_snippets}" in template.template_str


def test_registry_ato_v1_required_vars_excludes_policy_snippets(registry):
    template = registry.get("ato-v1")
    assert "policy_snippets" not in template.required_vars


def test_registry_ato_v1_required_vars_includes_risk_score(registry):
    template = registry.get("ato-v1")
    assert "risk_score" in template.required_vars


def test_registry_get_raises_for_unknown_id(registry):
    with pytest.raises(KeyError, match="not found"):
        registry.get("nonexistent-template")


def test_registry_validate_context_passes_when_all_vars_present(registry):
    template = registry.get("ato-v1")
    complete_vars = dict.fromkeys(template.required_vars, "test_value")
    # Should not raise
    registry.validate_context("ato-v1", complete_vars)


def test_registry_validate_context_raises_on_missing_var(registry):
    template = registry.get("ato-v1")
    # Provide all required vars except one
    required = list(template.required_vars)
    incomplete_vars = dict.fromkeys(required[1:], "value")
    with pytest.raises(ValueError, match="requires variables"):
        registry.validate_context("ato-v1", incomplete_vars)


def test_registry_registered_ids_returns_sorted_list(registry):
    ids = registry.registered_ids()
    assert isinstance(ids, list)
    assert "ato-v1" in ids
    assert ids == sorted(ids)


def test_registry_satisfies_prompt_registry_protocol(registry):
    assert isinstance(registry, PromptRegistry)
