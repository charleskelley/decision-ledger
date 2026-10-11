"""Behavioral tests for the ATO reasoner's shipped prompt templates.

The ATO plugin owns ``ato_reasoner/prompts/``. These tests load
that real directory through the framework's ``YamlPromptRegistry`` and pin:

- every template the registration allows is actually shipped;
- ``ato-v3`` carries, verbatim, the system prompt that used to be a constant
  inside ``app/gate/policy/gate.py`` (the move is what v3 exists for);
- ``ato-v3`` renders byte-identical user text to ``ato-v2`` for the same
  variables (the plan's "rendered text is byte-identical" contract);
- ``ato-v1``/``ato-v2`` fall back to the framework default system text, so
  they are retained for historical bundles, not as a rollback target.
"""

from __future__ import annotations

from app.gate.policy.gate import _render_prompt
from app.gate.policy.prompt_registry import DEFAULT_SYSTEM_TEXT, YamlPromptRegistry
from ato_reasoner.plugin import PLUGIN
from ato_reasoner.registry import ATO_REGISTRATION
from core.snippet import RetrievedSnippet

# The gate-level constant that lived in app/gate/policy/gate.py before the
# system prompt moved into the template. ato-v3 must reproduce it exactly so
# the text the LLM sees is unchanged by the refactor.
_OLD_GATE_SYSTEM_PROMPT = (
    "You evaluate authentication risk for the policy gate. "
    "Respond with a PolicyGateVerdict that conforms strictly to the schema."
)

_TEMPLATE_VARS = {
    "risk_score": "0.612",
    "risk_tier": "HIGH_VALUE",
    "auth_method": "PASSWORD",
    "outcome": "SUCCESS",
    "jurisdiction": "US",
    "top_signals": "ip_novelty=1 (SHAP +0.210), geo_novelty=1 (SHAP +0.150)",
    "impossible_travel": "True",
    "velocity_1min": "1",
    "velocity_5min": "3",
    "velocity_60min": "7",
    "device_novelty": "1.00",
    "ip_novelty": "1.00",
    "geo_novelty": "1.00",
    "sparse_history": "False",
}

_SNIPPETS = [
    RetrievedSnippet(
        document_id="NIST-800-63B",
        title="Digital Identity Guidelines",
        version="3",
        jurisdiction="US_FEDERAL",
        section_path="5.2.2",
        text="Verifiers SHALL implement rate limiting on {authenticator} attempts.",
        relevance_score=0.9,
        retrieval_path="rrf_only",
    )
]


def _registry() -> YamlPromptRegistry:
    return YamlPromptRegistry([PLUGIN.prompts_dir])


def test_ato_prompts_dir_ships_v1_v2_and_v3():
    assert {"ato-v1", "ato-v2", "ato-v3"} <= set(_registry().registered_ids())


def test_ato_registration_allowed_templates_are_all_shipped():
    registered = set(_registry().registered_ids())
    assert ATO_REGISTRATION.allowed_prompt_template_ids <= registered


def test_ato_v3_system_text_equals_former_gate_constant():
    assert _registry().get("ato-v3").system_text == _OLD_GATE_SYSTEM_PROMPT


def test_ato_v1_and_v2_fall_back_to_framework_default_system_text():
    registry = _registry()
    assert registry.get("ato-v1").system_text == DEFAULT_SYSTEM_TEXT
    assert registry.get("ato-v2").system_text == DEFAULT_SYSTEM_TEXT


def test_ato_v3_requires_same_variables_as_v2():
    registry = _registry()
    assert registry.get("ato-v3").required_vars == registry.get("ato-v2").required_vars


def test_ato_v3_renders_byte_identical_user_text_to_v2():
    registry = _registry()
    rendered_v2 = _render_prompt(
        registry.get("ato-v2").template_text, _TEMPLATE_VARS, _SNIPPETS
    )
    rendered_v3 = _render_prompt(
        registry.get("ato-v3").template_text, _TEMPLATE_VARS, _SNIPPETS
    )
    assert rendered_v3 == rendered_v2


def test_ato_v3_template_text_is_byte_identical_to_v2():
    # Stronger than the rendered check: the raw template block itself is the
    # v2 block, so no variable set can make the two user messages differ.
    registry = _registry()
    assert registry.get("ato-v3").template_text == registry.get("ato-v2").template_text
