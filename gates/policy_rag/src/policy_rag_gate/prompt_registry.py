"""YamlPromptRegistry — file-based PromptRegistry implementation.

Loads versioned prompt templates from the YAML files in each reasoner's
``prompts_dir`` (declared on its ``ReasonerPlugin``) at startup. This is the
same structural pattern as the policy corpus builder: directories of
structured files are parsed into one in-memory lookup — no external store
dependency.

YAML file format::

    template_id: example-v1
    version: 1.0.0
    description: >
        One-line description of what this template does.
    system: |
        Optional. The system message sent alongside the rendered template.
        When absent, DEFAULT_SYSTEM_TEXT (generic framework wording) is used.
    template: |
        Full template content.
        Use {var_name} for domain-provided variables.
        Use {policy_snippets} where the gate injects retrieved policy chunks.

Template IDs are derived from the YAML ``template_id`` field (falling back to
the filename stem). A file named ``example-v1.yaml`` with
``template_id: example-v1`` registers as ``"example-v1"``.

Template IDs are global across the deployment: the same ``template_id``
appearing in two reasoners' directories is a configuration error and fails
construction, because ``prompt_template_id`` is the audit key recorded in
every Decision Bundle.

``{policy_snippets}`` is excluded from ``required_vars`` — it is
framework-injected at render time, not a domain-provided variable.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import yaml

from policy_rag_gate.models import PromptTemplate

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

# Matches {placeholder} tokens in template strings.
_COMPILED_REGEX = re.compile(r"\{(\w+)\}")

# Framework-injected placeholder — excluded from required_vars validation.
_FRAMEWORK_VARS: frozenset[str] = frozenset({"policy_snippets"})

# System message used when a template YAML carries no ``system:`` key.
# Deliberately generic: the framework knows nothing about any domain, so any
# domain-specific framing belongs in the reasoner's template file.
DEFAULT_SYSTEM_TEXT = (
    "You are a policy gate. Evaluate the observation against the retrieved "
    "policy snippets and respond with a verdict that conforms strictly to the "
    "response schema."
)


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
        ValueError: If the template content is empty, or a ``system`` key is
            present but blank.
    """
    raw: dict = yaml.safe_load(path.read_text(encoding="utf-8"))

    template_str: str = raw["template"]
    if not template_str.strip():
        raise ValueError(f"Template file '{path.name}' has empty template content.")

    system_text: str = raw.get("system", DEFAULT_SYSTEM_TEXT)
    if not system_text.strip():
        raise ValueError(
            f"Template file '{path.name}' has an empty 'system' key; omit the "
            "key to use the framework default."
        )

    return PromptTemplate(
        template_id=raw.get("template_id", path.stem),
        version=raw["version"],
        description=raw["description"],
        template_text=template_str,
        system_text=system_text,
        required_vars=_extract_required_vars(template_str),
    )


class YamlPromptRegistry:
    """File-based PromptRegistry that loads ``*.yaml`` templates from directories.

    Templates are loaded once at startup. The prompts directories are the
    source of truth: adding a YAML file registers a new template. Templates
    are immutable once loaded — the registry does not support runtime updates.

    Satisfies the ``PromptRegistry`` protocol defined in
    ``policy_rag_gate.models.prompt``.

    Args:
        prompts_dirs: Directories to scan for ``*.yaml`` template files — one
            per composed reasoner plugin (its ``prompts_dir``). An empty
            sequence builds an empty registry (a deployment with no reasoners
            composed).

    Raises:
        ValueError: If any YAML file fails to parse or has empty content, or
            if the same ``template_id`` is declared by files in more than one
            place.
    """

    def __init__(self, prompts_dirs: Sequence[Path]) -> None:
        """Load all *.yaml templates from every directory into the registry."""
        self._templates: dict[str, PromptTemplate] = {}
        sources: dict[str, Path] = {}
        for prompts_dir in prompts_dirs:
            for path in sorted(prompts_dir.glob("*.yaml")):
                template = _load_template(path)
                template_id = template.template_id
                if template_id in sources:
                    raise ValueError(
                        f"Prompt template id '{template_id}' is declared twice: "
                        f"{sources[template_id]} and {path}. Template ids are "
                        "global across the deployment because they are the "
                        "audit key in every Decision Bundle."
                    )
                sources[template_id] = path
                self._templates[template_id] = template

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
