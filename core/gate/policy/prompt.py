"""PromptTemplate and PromptRegistry — policy-gate-specific prompt contracts.

Lives under ``core/gate/policy/`` because prompt templates are an
LLM-backed-gate concern. A rule engine has no prompt; a webhook gate has
no template registry. Universal gate contracts (``GateInput``,
``GateOutput``, ``GateVerdict``) at the framework level make no
assumption that gates have prompts.

PromptTemplate defines the immutable contract for a single versioned
prompt. PromptRegistry is a Protocol the app-layer implementation
satisfies. The reference implementation (YamlPromptRegistry in
``app/gate/policy/``) loads templates from YAML files at startup.

Design rationale:
    ``prompt_template_id`` in ``GateContext`` is the audit key — recorded
    in every ``DecisionBundle`` (under ``gate_input.prompt_template_id``)
    and queryable across the decision log. The policy gate resolves the
    full template content from the registry at render time. Keeping the
    template out of ``GateContext`` enforces immutability: a reasoner
    cannot inject arbitrary prompt content per-observation.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class PromptSnapshot(BaseModel):
    """Point-in-time snapshot of the prompt template used for a gate invocation.

    Stored verbatim inside ``PolicyGateInput`` to make the bundle
    self-contained for audit and replay. Because ``PromptTemplate`` is
    immutable once deployed this snapshot will match the live registry
    entry, but storing it here removes the dependency on registry
    availability for historical audit and compliance review.

    Combined with ``raw_event.gate_context.template_vars`` and
    ``retrieval_results`` (both already in the bundle), an auditor can
    fully verify that ``rendered_prompt`` was correctly constructed from
    the template without querying the registry.

    Attributes:
        template_id: Stable template identifier (e.g., ``"ato-v1"``).
            Matches ``GateContext.prompt_template_id`` on the Observation.
        version: Semantic version of the template at invocation time
            (e.g., ``"1.0.0"``).
        template_text: Full template content at invocation time. Contains
            ``{placeholder}`` tokens for domain variables and
            ``{policy_snippets}`` for retrieved policy context.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    template_id: str
    version: str
    template_text: str


class PromptTemplate(BaseModel):
    """Immutable versioned prompt template.

    Loaded from a YAML file at startup. Once loaded, a template is immutable
    for the lifetime of the process. Any change to a template requires a new
    version file and a new ``template_id`` — the old version remains in the
    registry for historical bundle replay.

    ``required_vars`` is derived at load time from the ``{placeholder}``
    tokens in ``template_text``, excluding the framework-injected
    ``{policy_snippets}`` placeholder (which the gate fills, not the domain).

    Attributes:
        template_id: Stable identifier used as the audit key in
            ``PolicyGateInput.prompt_template_id`` and as the lookup key
            in ``PromptRegistry``. Matches the filename stem of the source
            YAML (e.g., ``"ato-v1"`` from ``ato-v1.yaml``). Immutable once
            deployed — changing this value requires a new file.
        version: Semantic version of this template (e.g., ``"1.0.0"``).
            Recorded alongside ``template_id`` for fine-grained audit
            trails when a template series evolves across minor revisions.
        description: Human-readable description of what this template does
            and which domain it serves. Surfaced in registry listings.
        template_text: Full template content. Must contain
            ``{policy_snippets}`` where the gate injects retrieved policy
            chunks. May contain any number of ``{var_name}`` placeholders
            for domain-provided variables.
        required_vars: Variable names that must be present in
            ``GateContext.gate_config["template_vars"]`` when this
            template is rendered. Derived from placeholders in
            ``template_text`` by the registry loader — domain assemblers
            do not set this directly.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    template_id: str
    version: str
    description: str
    template_text: str
    required_vars: frozenset[str]


@runtime_checkable
class PromptRegistry(Protocol):
    """Protocol for template registries that the policy gate depends on.

    The policy gate receives a ``PromptRegistry`` at construction and uses
    it to resolve templates by ID and validate domain-provided variable
    sets.

    The reference implementation (``YamlPromptRegistry`` in
    ``app/gate/policy/``) satisfies this protocol by loading from YAML
    files at startup. A production implementation could back this with a
    database, secrets manager, or versioned object store — the gate is
    agnostic to the backing store as long as this protocol is satisfied.
    """

    def get(self, template_id: str) -> PromptTemplate:
        """Retrieve a template by ID.

        Args:
            template_id: The stable template identifier.

        Returns:
            The ``PromptTemplate`` registered under this ID.

        Raises:
            KeyError: If ``template_id`` is not registered.
        """
        ...

    def validate_context(self, template_id: str, template_vars: dict[str, str]) -> None:
        """Validate that ``template_vars`` satisfies the template's required vars.

        Called by the gate before rendering to catch missing variables
        early, producing a clear error rather than a silent ``{var_name}``
        literal in the rendered prompt.

        Args:
            template_id: The template to validate against.
            template_vars: Variable values to check against ``required_vars``.

        Raises:
            KeyError: If ``template_id`` is not registered.
            ValueError: If any required variable is absent from
                ``template_vars``.
        """
        ...
