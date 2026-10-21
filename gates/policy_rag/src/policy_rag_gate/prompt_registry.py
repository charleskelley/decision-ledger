"""YamlPromptRegistry — file-based PromptRegistry implementation.

Loads versioned prompt templates from the YAML files in each reasoner's
``prompts_dir`` (declared on its ``ReasonerPlugin``) when the gate is built,
and adds the templates of a reasoner registered without a plugin, from its
directory under the gate's asset root, when the gate admits that reasoner's
binding (``add``, DR-33). This is the same structural pattern as the policy
corpus builder: directories of structured files are parsed into one
in-memory lookup — no external store dependency.

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

Template IDs are global across the deployment and immutable: the same
``template_id`` appearing in two reasoners' directories is a configuration
error that fails construction (or refuses the ``add``), because
``prompt_template_id`` is the audit key recorded in every Decision Bundle.

``{policy_snippets}`` is excluded from ``required_vars`` — it is
framework-injected at render time, not a domain-provided variable.
"""

from __future__ import annotations

import re
import threading
from typing import TYPE_CHECKING

import yaml

from policy_rag_gate.models import PromptTemplate

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
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
        ValueError: If the file is not a mapping, the template content is
            empty or not text, or a ``system`` key is present but blank or not
            text.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Template file '{path.name}' is not a mapping of fields.")

    template_str = raw["template"]
    if not isinstance(template_str, str):
        raise ValueError(
            f"Template file '{path.name}' has a template that is not text."
        )
    if not template_str.strip():
        raise ValueError(f"Template file '{path.name}' has empty template content.")

    system_text = raw.get("system", DEFAULT_SYSTEM_TEXT)
    if system_text is None or (
        isinstance(system_text, str) and not system_text.strip()
    ):
        raise ValueError(
            f"Template file '{path.name}' has an empty 'system' key; omit the "
            "key to use the framework default."
        )
    if not isinstance(system_text, str):
        raise ValueError(
            f"Template file '{path.name}' has a 'system' key that is not text."
        )

    return PromptTemplate(
        template_id=raw.get("template_id", path.stem),
        version=raw["version"],
        description=raw["description"],
        template_text=template_str,
        system_text=system_text,
        required_vars=_extract_required_vars(template_str),
    )


def _declared_twice(template_id: str, first: Path, second: Path) -> ValueError:
    return ValueError(
        f"Prompt template id '{template_id}' is declared twice: {first} and "
        f"{second}. Template ids are global across the deployment because they "
        "are the audit key in every Decision Bundle."
    )


def load_templates(prompts_dir: Path) -> dict[str, tuple[PromptTemplate, Path]]:
    """Load every ``*.yaml`` template in one directory (not recursive).

    Args:
        prompts_dir: A reasoner's directory of prompt templates. A missing
            directory holds no templates.

    Returns:
        ``template_id`` → the template and the file it came from, in file
        name order.

    Raises:
        ValueError: Two files in the directory declare one ``template_id``,
            or a file's template content is empty.
        KeyError, TypeError, yaml.YAMLError, pydantic.ValidationError: A
            file is malformed (a missing key, not a mapping, not YAML, or a
            value of the wrong type).
    """
    loaded: dict[str, tuple[PromptTemplate, Path]] = {}
    for path in sorted(prompts_dir.glob("*.yaml")):
        template = _load_template(path)
        if template.template_id in loaded:
            raise _declared_twice(
                template.template_id, loaded[template.template_id][1], path
            )
        loaded[template.template_id] = (template, path)
    return loaded


class YamlPromptRegistry:
    """File-based PromptRegistry that loads ``*.yaml`` templates from directories.

    Templates are loaded when the registry is built, from the prompts
    directories of the composed reasoner plugins, and ``add`` adds the
    templates of a reasoner admitted later (DR-33). Add-only: a template,
    once registered, never changes or leaves, and a ``template_id`` is
    registered once. Reads never wait for an ``add``: it swaps in a new map.

    Satisfies the ``PromptRegistry`` protocol defined in
    ``policy_rag_gate.models.prompt``.

    Args:
        prompts_dirs: Directories to scan for ``*.yaml`` template files — one
            per composed reasoner plugin (its ``prompts_dir``). An empty
            sequence builds an empty registry (a deployment with no reasoners
            composed).

    Raises:
        ValueError: If a template file has empty content, or if the same
            ``template_id`` is declared by files in more than one place.
        KeyError, TypeError, yaml.YAMLError, pydantic.ValidationError: A
            template file is malformed (see ``load_templates``).
    """

    def __init__(self, prompts_dirs: Sequence[Path]) -> None:
        """Load all *.yaml templates from every directory into the registry."""
        self._templates: Mapping[str, PromptTemplate] = {}
        self._sources: Mapping[str, Path] = {}
        self._lock = threading.Lock()
        for prompts_dir in prompts_dirs:
            self.add(load_templates(prompts_dir))

    def add(self, templates: Mapping[str, tuple[PromptTemplate, Path]]) -> None:
        """Register templates loaded with ``load_templates``, all or none.

        Args:
            templates: ``template_id`` → template and source file.

        Raises:
            ValueError: A ``template_id`` is registered already (nothing is
                added).
        """
        with self._lock:
            for template_id, (_, path) in templates.items():
                if template_id in self._sources:
                    raise _declared_twice(template_id, self._sources[template_id], path)
            self._templates = {
                **self._templates,
                **{tid: template for tid, (template, _) in templates.items()},
            }
            self._sources = {
                **self._sources,
                **{tid: path for tid, (_, path) in templates.items()},
            }

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
        templates = self._templates
        try:
            return templates[template_id]
        except KeyError:
            registered = sorted(templates)
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
