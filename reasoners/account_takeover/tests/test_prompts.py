"""Behavioral tests for the ATO reasoner's shipped prompt templates.

The ATO plugin owns ``ato_reasoner/prompts/``. These tests load
that real directory through the RAG policy gate's ``YamlPromptRegistry`` and
pin:

- ``ato-v1`` through ``ato-v4`` are shipped (the gate authorizes exactly the
  templates ATO ships; see ``test_registry.py``), and ``ato-v4`` is the one
  ATO names unless told otherwise;
- ``ato-v3`` carries, verbatim, the system prompt that used to be a constant
  inside the policy gate's code (the move is what v3 exists for);
- ``ato-v3`` renders byte-identical user text to ``ato-v2`` for the same
  variables (the plan's "rendered text is byte-identical" contract);
- ``ato-v1``/``ato-v2`` fall back to the framework default system text, so
  they are retained for historical bundles, not as a rollback target;
- ``ato-v4`` (DR-34) renders the evidence by authority tier and asks for the
  tiered verdict with the conflict/gap protocol, needs exactly ``ato-v3``'s
  variables, and keeps ``ato-v3``'s event section; ``ato-v3`` still renders
  the numbered list exactly as before.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ato_reasoner.assembler import DEFAULT_TEMPLATE_ID
from ato_reasoner.plugin import PLUGIN
from ato_reasoner.settings import SELECTABLE_TEMPLATE_IDS, AtoSettings
from policy_rag_gate.gate import _render_prompt, render_evidence
from policy_rag_gate.models import RetrievedContext, content_hash
from policy_rag_gate.prompt_registry import DEFAULT_SYSTEM_TEXT, YamlPromptRegistry

# The gate-level constant that lived in the policy gate's code before the
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

_TEXT = "Verifiers SHALL implement rate limiting on {authenticator} attempts."
_SNIPPETS = [
    RetrievedContext(
        corpus_id="ato-policy",
        provider="reference",
        tier=1,
        document_id="NIST-800-63B",
        title="Digital Identity Guidelines",
        version="3",
        jurisdiction="US_FEDERAL",
        section_path="5.2.2",
        text=_TEXT,
        content_hash=content_hash(_TEXT),
        rank=1,
        provenance_level="pinned_version",
    )
]


def _rendered(template_id: str) -> str:
    template = _registry().get(template_id)
    return _render_prompt(
        template.template_text,
        _TEMPLATE_VARS,
        render_evidence(_SNIPPETS, template.evidence_layout),
    )


def _registry() -> YamlPromptRegistry:
    return YamlPromptRegistry([PLUGIN.prompts_dir])


def test_ato_prompts_dir_ships_v1_through_v4():
    assert {"ato-v1", "ato-v2", "ato-v3", "ato-v4"} <= set(_registry().registered_ids())


def test_ato_names_ato_v4_unless_told_otherwise():
    assert DEFAULT_TEMPLATE_ID == "ato-v4"
    assert AtoSettings(_env_file=None).template_id == "ato-v4"  # type: ignore[call-arg]


@pytest.mark.parametrize("template_id", SELECTABLE_TEMPLATE_IDS)
def test_every_template_ato_can_be_told_to_name_renders(template_id):
    rendered = _rendered(template_id)

    assert "rate limiting on {authenticator} attempts" in rendered
    assert "{policy_snippets}" not in rendered


def test_ato_v1_cannot_be_named_since_it_no_longer_renders():
    with pytest.raises(KeyError):
        _rendered("ato-v1")
    with pytest.raises(ValidationError, match="ato-v1"):
        AtoSettings(_env_file=None, template_id="ato-v1")  # type: ignore[call-arg]


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
    assert _rendered("ato-v3") == _rendered("ato-v2")


def test_ato_v3_template_text_is_byte_identical_to_v2():
    # Stronger than the rendered check: the raw template block itself is the
    # v2 block, so no variable set can make the two user messages differ.
    registry = _registry()
    assert registry.get("ato-v3").template_text == registry.get("ato-v2").template_text


# ---------------------------------------------------------------------------
# ato-v4 — the conflict/gap protocol (DR-34)
# ---------------------------------------------------------------------------


def test_ato_v3_keeps_the_numbered_list_and_v4_renders_tiers():
    registry = _registry()

    assert registry.get("ato-v3").evidence_layout == "numbered_list"
    assert registry.get("ato-v4").evidence_layout == "tiered_blocks"
    assert "[1] Digital Identity Guidelines (v3) — US_FEDERAL" in _rendered("ato-v3")
    rendered = _rendered("ato-v4")
    assert "--- Tier 1 (corpora: ato-policy) ---" in rendered
    assert "Corpus: ato-policy | Tier: 1 | Document: NIST-800-63B" in rendered


def test_ato_v4_needs_exactly_the_variables_ato_v3_does():
    registry = _registry()
    assert registry.get("ato-v4").required_vars == registry.get("ato-v3").required_vars


def test_ato_v4_keeps_the_event_section_of_ato_v3():
    registry = _registry()
    v3 = registry.get("ato-v3").template_text
    v4 = registry.get("ato-v4").template_text
    event = v3[v3.index("## Authentication Event") : v3.index("## Retrieved")]

    assert event in v4


def test_ato_v4_asks_for_the_tiered_verdict_and_its_protocol_fields():
    template = _registry().get("ato-v4")

    assert "TieredPolicyGateVerdict" in template.system_text
    for field in ("governing_corpus_id", "conflict_detected", "evidence_sufficient"):
        assert field in template.template_text
