"""PolicyGateInput — LLM-backed policy gate's input subclass.

Extends the universal ``GateInput`` with the typed invocation context
the policy gate captures: model identifier, prompt template metadata,
retrieval corpus version, the rendered prompt itself, and a verbatim
prompt-template snapshot for offline audit.

Every field is a typed Pydantic attribute. The framework does not use
``dict[str, Any]`` escape hatches; if a future LLM-backed gate variant
needs an additional field, it goes here as a typed attribute (or in a
sibling subclass).
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict

from core.gate.input import GateInput
from core.gate.policy.prompt import (
    PromptSnapshot,  # noqa: TC001 (runtime Pydantic field)
)


class PolicyGateInput(GateInput):
    """Input to the LLM-backed policy gate.

    Attributes:
        gate_id: Discriminator value ``"policy"``.
        model_version: LLM model identifier used for this invocation
            (e.g., ``"gpt-4o-2024-08-06"``).
        prompt_template_id: Stable identifier of the prompt template
            resolved from the registry.
        prompt_template_version: Semantic version of the resolved
            template at invocation time.
        corpus_version: Version label of the retrieval corpus this
            invocation consumed. Sourced from the orchestrator
            (``app.main.Settings.corpus_version``) — infrastructure-side
            metadata that the gate records for audit.
        rendered_prompt: Full prompt as sent to the LLM, with
            ``template_vars`` and retrieved snippets already substituted.
        prompt_snapshot: Verbatim snapshot of the prompt template at
            invocation time. Combined with the ``template_vars`` field
            below and ``DecisionBundle.retrieval_results``, an auditor
            can verify that ``rendered_prompt`` was correctly constructed
            without querying the registry.
        template_vars: Domain-rendered substitutions actually used in
            this invocation. Mirrors
            ``raw_event.gate_context.gate_config["template_vars"]`` but
            denormalized here so a ``PolicyGateInput`` is self-contained
            for audit reading.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    gate_id: Literal["policy"] = "policy"
    model_version: str
    prompt_template_id: str
    prompt_template_version: str
    corpus_version: str
    rendered_prompt: str
    prompt_snapshot: PromptSnapshot
    template_vars: dict[str, str]
