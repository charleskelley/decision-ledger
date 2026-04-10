"""LLM policy gate — structured JSON reasoning over retrieved policy evidence.

Constructs prompts from versioned YAML templates (app/policy_gate/prompts/v{n}.yaml),
calls the LLM API, and validates the response against the PolicyGateOutput Pydantic
schema. Schema validation failures route to HOLD — never to silent enforcement.

Prompt versioning rules:
    - Prompt files are immutable once created. Never modify an existing prompt YAML.
    - A behavior change requires a new version file (v2.yaml, v3.yaml, ...).
    - The active prompt version is recorded in every Decision Bundle.
    - CI blocks merges that activate a new version without a passing eval gate run.
"""

from app.policy_gate.gate import GateResult, PolicyGate
from app.policy_gate.prompt_registry import YamlPromptRegistry

__all__ = ["GateResult", "PolicyGate", "YamlPromptRegistry"]
