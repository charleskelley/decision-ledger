"""Behavioral tests for YamlPromptRegistry and PromptTemplate contracts.

Tests are split into two groups:
- PromptTemplate contract tests: pure Pydantic model validation, no filesystem.
- YamlPromptRegistry tests: use small fixture YAMLs written to ``tmp_path``.
  They do not depend on any reasoner's real prompt files, so framework
  behavior is pinned independently of what a given reasoner ships.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from policy_rag_gate.models import PromptRegistry, PromptTemplate
from policy_rag_gate.prompt_registry import (
    DEFAULT_SYSTEM_TEXT,
    YamlPromptRegistry,
    _extract_required_vars,
    load_templates,
)

if TYPE_CHECKING:
    from pathlib import Path

_FIXTURE_V1 = """\
template_id: fixture-v1
version: 1.0.0
description: >
  Fixture template with two domain variables and no system key.
template: |
  Score: {risk_score}
  Tier: {risk_tier}

  Evidence:
  {policy_snippets}
"""

_FIXTURE_V2_WITH_SYSTEM = """\
template_id: fixture-v2
version: 2.0.0
description: >
  Fixture template that carries its own system message.
system: >-
  You are the fixture gate. Be terse.
template: |
  Score: {risk_score}
  {policy_snippets}
"""

_FIXTURE_NO_ID = """\
version: 0.1.0
description: Fixture template that relies on the filename stem for its id.
template: |
  Hello {name}.
  {policy_snippets}
"""


def _write(directory: Path, filename: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def prompts_dir(tmp_path: Path) -> Path:
    """A prompts directory holding fixture-v1 (no system) and fixture-v2."""
    directory = tmp_path / "prompts"
    _write(directory, "fixture-v1.yaml", _FIXTURE_V1)
    _write(directory, "fixture-v2.yaml", _FIXTURE_V2_WITH_SYSTEM)
    return directory


@pytest.fixture
def registry(prompts_dir: Path) -> YamlPromptRegistry:
    return YamlPromptRegistry([prompts_dir])


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


def _template(**overrides: object) -> PromptTemplate:
    fields: dict[str, object] = {
        "template_id": "test-v1",
        "version": "1.0.0",
        "description": "Test template.",
        "template_text": "Hello {name}.",
        "system_text": "You are a test gate.",
        "required_vars": frozenset({"name"}),
    }
    fields.update(overrides)
    return PromptTemplate(**fields)  # type: ignore[arg-type]


def test_prompt_template_required_vars_is_frozenset():
    t = _template()
    assert isinstance(t.required_vars, frozenset)
    assert "name" in t.required_vars


def test_prompt_template_is_immutable():
    t = _template()
    with pytest.raises(ValidationError):
        t.template_id = "modified"


def test_prompt_template_requires_system_text():
    with pytest.raises(ValidationError):
        PromptTemplate(
            template_id="test-v1",
            version="1.0.0",
            description="Test.",
            template_text="Hello {name}.",
            required_vars=frozenset({"name"}),
        )  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# YamlPromptRegistry — loading from fixture directories
# ---------------------------------------------------------------------------


def test_registry_loads_every_yaml_in_directory(registry: YamlPromptRegistry):
    assert registry.registered_ids() == ["fixture-v1", "fixture-v2"]


def test_registry_reads_template_id_and_version_from_yaml(registry):
    template = registry.get("fixture-v1")
    assert template.template_id == "fixture-v1"
    assert template.version == "1.0.0"


def test_registry_falls_back_to_filename_stem_for_template_id(tmp_path: Path):
    directory = tmp_path / "prompts"
    _write(directory, "stem-only-v9.yaml", _FIXTURE_NO_ID)
    registry = YamlPromptRegistry([directory])
    assert registry.registered_ids() == ["stem-only-v9"]


def test_registry_required_vars_excludes_policy_snippets(registry):
    template = registry.get("fixture-v1")
    assert "policy_snippets" not in template.required_vars
    assert template.required_vars == frozenset({"risk_score", "risk_tier"})


def test_registry_uses_default_system_text_when_yaml_has_no_system_key(registry):
    assert registry.get("fixture-v1").system_text == DEFAULT_SYSTEM_TEXT


def test_registry_honors_explicit_system_key(registry):
    expected = "You are the fixture gate. Be terse."
    assert registry.get("fixture-v2").system_text == expected


def test_default_system_text_carries_no_domain_wording():
    lowered = DEFAULT_SYSTEM_TEXT.lower()
    for domain_word in ("authentication", "login", "ato", "account", "fraud"):
        assert domain_word not in lowered


def test_registry_rejects_blank_system_key(tmp_path: Path):
    directory = tmp_path / "prompts"
    _write(
        directory,
        "blank-system.yaml",
        "template_id: blank-system\nversion: 1.0.0\ndescription: x\n"
        'system: "   "\ntemplate: |\n  {policy_snippets}\n',
    )
    with pytest.raises(ValueError, match="empty 'system' key"):
        YamlPromptRegistry([directory])


def test_registry_rejects_empty_template_content(tmp_path: Path):
    directory = tmp_path / "prompts"
    _write(
        directory,
        "empty.yaml",
        'template_id: empty\nversion: 1.0.0\ndescription: x\ntemplate: "   "\n',
    )
    with pytest.raises(ValueError, match="empty template content"):
        YamlPromptRegistry([directory])


def test_registry_with_no_directories_is_empty():
    registry = YamlPromptRegistry([])
    assert registry.registered_ids() == []


def test_registry_merges_templates_across_directories(tmp_path: Path):
    dir_a = tmp_path / "reasoner-a" / "prompts"
    dir_b = tmp_path / "reasoner-b" / "prompts"
    _write(dir_a, "fixture-v1.yaml", _FIXTURE_V1)
    _write(dir_b, "fixture-v2.yaml", _FIXTURE_V2_WITH_SYSTEM)
    registry = YamlPromptRegistry([dir_a, dir_b])
    assert registry.registered_ids() == ["fixture-v1", "fixture-v2"]


def test_registry_rejects_same_template_id_in_two_directories(tmp_path: Path):
    dir_a = tmp_path / "reasoner-a" / "prompts"
    dir_b = tmp_path / "reasoner-b" / "prompts"
    path_a = _write(dir_a, "fixture-v1.yaml", _FIXTURE_V1)
    path_b = _write(dir_b, "renamed-but-same-id.yaml", _FIXTURE_V1)
    with pytest.raises(ValueError, match="declared twice") as exc_info:
        YamlPromptRegistry([dir_a, dir_b])
    message = str(exc_info.value)
    assert "fixture-v1" in message
    assert str(path_a) in message
    assert str(path_b) in message


# ---------------------------------------------------------------------------
# YamlPromptRegistry — lookup and validation semantics
# ---------------------------------------------------------------------------


def test_registry_get_raises_for_unknown_id(registry):
    with pytest.raises(KeyError, match="not found"):
        registry.get("nonexistent-template")


def test_registry_validate_context_passes_when_all_vars_present(registry):
    registry.validate_context("fixture-v1", {"risk_score": "0.5", "risk_tier": "STD"})


def test_registry_validate_context_raises_on_missing_var(registry):
    with pytest.raises(ValueError, match="requires variables"):
        registry.validate_context("fixture-v1", {"risk_score": "0.5"})


def test_registry_registered_ids_returns_sorted_list(tmp_path: Path):
    directory = tmp_path / "prompts"
    _write(directory, "zed.yaml", _FIXTURE_V1.replace("fixture-v1", "zed-v1"))
    _write(directory, "alpha.yaml", _FIXTURE_V1.replace("fixture-v1", "alpha-v1"))
    registry = YamlPromptRegistry([directory])
    ids = registry.registered_ids()
    assert isinstance(ids, list)
    assert ids == ["alpha-v1", "zed-v1"]


def test_registry_satisfies_prompt_registry_protocol(registry):
    assert isinstance(registry, PromptRegistry)


# ---------------------------------------------------------------------------
# YamlPromptRegistry — add-only registration after build (DR-33)
# ---------------------------------------------------------------------------


def test_load_templates_reads_one_directory_with_each_source(tmp_path: Path):
    path = _write(tmp_path / "prompts", "fixture-v1.yaml", _FIXTURE_V1)

    loaded = load_templates(tmp_path / "prompts")

    assert list(loaded) == ["fixture-v1"]
    template, source = loaded["fixture-v1"]
    assert (template.version, source) == ("1.0.0", path)
    assert load_templates(tmp_path / "missing") == {}


def test_load_templates_refuses_one_id_declared_twice_in_a_directory(tmp_path: Path):
    directory = tmp_path / "prompts"
    _write(directory, "a.yaml", _FIXTURE_V1)
    _write(directory, "b.yaml", _FIXTURE_V1)

    with pytest.raises(ValueError, match="'fixture-v1' is declared twice"):
        load_templates(directory)


def test_add_registers_new_templates(registry, tmp_path: Path):
    directory = tmp_path / "later"
    _write(directory, "late.yaml", _FIXTURE_V1.replace("fixture-v1", "late-v1"))

    registry.add(load_templates(directory))

    assert registry.get("late-v1").template_id == "late-v1"
    assert "fixture-v1" in registry.registered_ids()


def test_add_is_all_or_nothing_and_never_replaces_a_template(registry, tmp_path: Path):
    directory = tmp_path / "later"
    _write(directory, "a.yaml", _FIXTURE_V1.replace("fixture-v1", "fresh-v1"))
    _write(directory, "b.yaml", _FIXTURE_V1.replace("Score:", "Changed:"))
    before = registry.get("fixture-v1")

    with pytest.raises(ValueError, match="'fixture-v1' is declared twice"):
        registry.add(load_templates(directory))

    assert registry.get("fixture-v1") is before
    assert "fresh-v1" not in registry.registered_ids()
