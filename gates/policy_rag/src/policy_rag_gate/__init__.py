"""RAG policy gate — hybrid retrieval plus a structured LLM verdict (DR-28).

A workspace member that plugs into DecisionLedger through the
``decisionledger.gates`` entry point ``policy``
(``policy_rag_gate.plugin:PLUGIN``). The framework never imports this
package; it knows the gate only through ``GatePlugin`` / ``Gate`` and the
universal ``GateOutput`` the gate returns.

The gate runs hybrid retrieval over the submitting reasoner's corpus,
constructs prompts from versioned YAML templates (each reasoner plugin ships
its own ``prompts/`` directory; a reasoner registered without a plugin ships
its prompts and corpus under the gate's asset root,
``POLICY_RAG_GATE_ASSETS_ROOT``, DR-33), calls the LLM API, and validates the
response against the ``PolicyGateVerdict`` schema. Schema validation
failures route to HOLD — never to silent enforcement. Its own typed models
(``PolicyGateSnapshot``, ``PolicyGateExtra``, ...) live in
``policy_rag_gate.models`` and are serialized into the output's opaque
``evidence_snapshot`` / ``extra``.

The gate owns its infrastructure: the ``policy_rag`` Postgres schema (chunk
table and the vector extension, created by its own DDL in ``sql/``), the
Elasticsearch index (mapping in ``es/``, service in the member's
``compose.yaml``), the embedding and cross-encoder models, the LLM adapters
(``llm/``), the corpus loader, and its eval (retrieval, faithfulness,
citation, and the LLM judge, in ``eval/``).

Prompt versioning rules:
    - Prompt files are immutable once created. Never modify an existing
      prompt YAML.
    - A behavior change requires a new version file (v2.yaml, v3.yaml, ...).
    - The active prompt version is recorded in every Decision Bundle the
      gate decides, under
      ``gate_output.evidence_snapshot["prompt_template_version"]``.
    - CI blocks merges that activate a new version without a passing
      eval gate run.

Public API:
    PolicyGate:          The runtime gate.
    POLICY_GATE_ID:      ``"policy"``.
    PolicyGateConfig:    The gate's ``config_model`` (its ``gate_config``).
    ReasonerProfile:     What one reasoner may submit to the gate.
    YamlPromptRegistry:  File-based prompt registry.

The plugin itself (``policy_rag_gate.plugin``) is not re-exported: importing
it loads the embedding stack, and the entry point names it directly.
"""

from policy_rag_gate.config import PolicyGateConfig, ReasonerProfile
from policy_rag_gate.gate import POLICY_GATE_ID, PolicyGate
from policy_rag_gate.prompt_registry import YamlPromptRegistry

__all__ = [
    "POLICY_GATE_ID",
    "PolicyGate",
    "PolicyGateConfig",
    "ReasonerProfile",
    "YamlPromptRegistry",
]
