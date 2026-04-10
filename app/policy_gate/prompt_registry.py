"""YamlPromptRegistry — file-based PromptRegistry implementation.

Loads versioned prompt templates from YAML files in ``app/policy_gate/prompts/``
at startup. This is the same structural pattern as the policy corpus builder: a
directory of structured files is parsed into an in-memory lookup — no external
store dependency.

YAML file format::

    template_id: ato-v1
    version: 1.0.0
    description: >
        One-line description of what this template does.
    template: |
        Full template content.
        Use {var_name} for domain-provided variables.
        Use {policy_snippets} where the gate injects retrieved policy chunks.

Template IDs are derived from the YAML ``template_id`` field (falling back to
the filename stem). A file named ``ato-v1.yaml`` with ``template_id: ato-v1``
registers as ``"ato-v1"``.

``{policy_snippets}`` is excluded from ``required_vars`` — it is
framework-injected at render time, not a domain-provided variable.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from core.gate import PromptTemplate

_PROMPTS_DIR = Path(__file__).parent / "prompts"

# Matches {placeholder} tokens in template strings.
_COMPILED_REGEX = re.compile(r"\{(\w+)\}")

# Framework-injected placeholder — excluded from required_vars validation.
_FRAMEWORK_VARS: frozenset[str] = frozenset({"policy_snippets"})


def _extract_required_vars(template_str: str) -> frozenset[str]:
    """Extract domain-required variable names from a template string.

    Finds all ``{placeholder}`` tokens and excludes framework-injected ones
    (``policy_snippets``).

    Args:
        template_str: Raw template content.

    Returns:
        Frozenset of variable names the domain assembler must supply.
    """
    all_vars = frozenset(_COMPILED_REGEX.findall(template_str))
    return all_vars - _FRAMEWORK_VARS


def _load_template(path: Path) -> PromptTemplate:
    """Parse a single YAML template file into a PromptTemplate.

    Args:
        path: Path to the ``.yaml`` file.

    Returns:
        Parsed and validated ``PromptTemplate``.

    Raises:
        KeyError: If the YAML is missing a required field.
        ValueError: If the template content is empty.
    """
    raw: dict = yaml.safe_load(path.read_text(encoding="utf-8"))

    template_str: str = raw["template"]
    if not template_str.strip():
        raise ValueError(f"Template file '{path.name}' has empty template content.")

    return PromptTemplate(
        template_id=raw.get("template_id", path.stem),
        version=raw["version"],
        description=raw["description"],
        template_str=template_str,
        required_vars=_extract_required_vars(template_str),
    )


class YamlPromptRegistry:
    """File-based PromptRegistry that loads from ``app/policy_gate/prompts/*.yaml``.

    Templates are loaded once at startup. The prompts directory is the source
    of truth: adding a YAML file registers a new template. Templates are
    immutable once loaded — the registry does not support runtime updates.

    Satisfies the ``PromptRegistry`` protocol defined in ``core/policy/prompt.py``.

    Args:
        prompts_dir: Directory to scan for ``*.yaml`` template files.
            Defaults to ``app/policy_gate/prompts/``. Override in tests
            to point at a fixture directory.

    Raises:
        ValueError: If any YAML file fails to parse or has empty content.
    """

    def __init__(self, prompts_dir: Path = _PROMPTS_DIR) -> None:
        """Load all *.yaml templates from prompts_dir into the in-memory registry."""
        self._templates: dict[str, PromptTemplate] = {}
        for path in sorted(prompts_dir.glob("*.yaml")):
            template = _load_template(path)
            self._templates[template.template_id] = template

    def get(self, template_id: str) -> PromptTemplate:
        """Retrieve a template by ID.

        Args:
            template_id: Stable template identifier (matches ``template_id``
                field in the source YAML, defaulting to filename stem).

        Returns:
            The ``PromptTemplate`` registered under this ID.

        Raises:
            KeyError: If ``template_id`` is not registered.
        """
        try:
            return self._templates[template_id]
        except KeyError:
            registered = sorted(self._templates)
            raise KeyError(
                f"Prompt template '{template_id}' not found. "
                f"Registered templates: {registered}"
            ) from None

    def validate_context(self, template_id: str, template_vars: dict[str, str]) -> None:
        """Validate that ``template_vars`` satisfies the template's ``required_vars``.

        Args:
            template_id: Template to validate against.
            template_vars: Variable values to check against ``required_vars``.

        Raises:
            KeyError: If ``template_id`` is not registered.
            ValueError: If any required variable is absent from
                ``template_vars``.
        """
        template = self.get(template_id)
        missing = template.required_vars - template_vars.keys()
        if missing:
            raise ValueError(
                f"Template '{template_id}' requires variables "
                f"{sorted(missing)} but they were not provided in "
                f"template_vars. Provided: {sorted(template_vars)}"
            )

    def registered_ids(self) -> list[str]:
        """Return sorted list of all registered template IDs.

        Returns:
            Sorted list of template ID strings.
        """
        return sorted(self._templates)
