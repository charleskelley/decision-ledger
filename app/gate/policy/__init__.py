"""LLM-backed policy gate — structured JSON reasoning over retrieved evidence.

The reference ``Gate`` (DR-28), registered through the plugin protocol like
any other gate: ``app.gate.policy.plugin:PLUGIN`` is published under the
``decisionledger.gates`` entry-point group as ``policy``. That entry point is
interim — it sits on the framework's root package until Phase 1b moves this
gate (with ``app/retrieval`` and ``app/llm``) into its own workspace member.

The gate runs hybrid retrieval over the submitting reasoner's corpus,
constructs prompts from versioned YAML templates (each reasoner plugin ships
its own ``prompts/`` directory), calls the LLM API, and validates the
response against the ``PolicyGateVerdict`` schema. Schema validation
failures route to HOLD — never to silent enforcement. It returns the
universal ``GateOutput``; its own typed models (``PolicyGateSnapshot``,
``PolicyGateExtra``) live in ``core.gate.policy`` and are serialized into
the output's opaque ``evidence_snapshot`` / ``extra``.

Prompt versioning rules:
    - Prompt files are immutable once created. Never modify an existing
      prompt YAML.
    - A behavior change requires a new version file (v2.yaml, v3.yaml, ...).
    - The active prompt version is recorded in every Decision Bundle the
      gate decides, under
      ``gate_output.evidence_snapshot["prompt_template_version"]``.
    - CI blocks merges that activate a new version without a passing
      eval gate run.
"""

from app.gate.policy.gate import POLICY_GATE_ID, PolicyGate
from app.gate.policy.prompt_registry import YamlPromptRegistry

__all__ = ["POLICY_GATE_ID", "PolicyGate", "YamlPromptRegistry"]
