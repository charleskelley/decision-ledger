"""LLM-backed policy gate — structured JSON reasoning over retrieved evidence.

Constructs prompts from versioned YAML templates (under ``prompts/``),
calls the LLM API, and validates the response against the
``PolicyGateVerdict`` Pydantic schema. Schema validation failures route
to HOLD — never to silent enforcement.

Per DR-20, the contract types this gate produces (``PolicyGateInput``,
``PolicyGateOutput``, ``PolicyGateVerdict``) live in
``core.gate.policy``. This module is the runtime orchestrator that
populates them.

Prompt versioning rules:
    - Prompt files are immutable once created. Never modify an existing
      prompt YAML.
    - A behavior change requires a new version file (v2.yaml, v3.yaml, ...).
    - The active prompt version is recorded in every Decision Bundle
      under ``gate_input.prompt_template_version``.
    - CI blocks merges that activate a new version without a passing
      eval gate run.
"""

from app.gate.policy.gate import GateResult, PolicyGate
from app.gate.policy.prompt_registry import YamlPromptRegistry

__all__ = ["GateResult", "PolicyGate", "YamlPromptRegistry"]
