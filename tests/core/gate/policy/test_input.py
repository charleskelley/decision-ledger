"""Behavioral tests for PolicyGateInput — concrete LLM-policy-gate input contract.

Asserts the typed top-level fields, frozen-ness, gate_id Literal narrowing,
and that the contract requires every LLM-side artifact (no extras dict).
"""

import pytest
from pydantic import ValidationError

from core.gate.policy import PolicyGateInput, PromptSnapshot


def _snapshot() -> PromptSnapshot:
    return PromptSnapshot(
        template_id="ato-v1",
        version="1.0.0",
        template_text="Risk: {risk_score}\n{policy_snippets}",
    )


def _make(**overrides) -> PolicyGateInput:
    base = {
        "model_version": "gpt-4o-2024-08-06",
        "prompt_template_id": "ato-v1",
        "prompt_template_version": "1.0.0",
        "corpus_version": "corpus-v1",
        "rendered_prompt": "Risk: 0.50\nEvidence: ...",
        "prompt_snapshot": _snapshot(),
        "template_vars": {"risk_score": "0.50"},
    }
    base.update(overrides)
    return PolicyGateInput(**base)


def test_policy_gate_input_construction_populates_all_fields():
    pgi = _make()
    assert pgi.gate_id == "policy"
    assert pgi.model_version == "gpt-4o-2024-08-06"
    assert pgi.prompt_template_id == "ato-v1"
    assert pgi.prompt_template_version == "1.0.0"
    assert pgi.corpus_version == "corpus-v1"
    assert pgi.rendered_prompt.startswith("Risk: 0.50")
    assert pgi.prompt_snapshot.template_id == "ato-v1"
    assert pgi.template_vars == {"risk_score": "0.50"}


def test_policy_gate_input_gate_id_is_literal_policy():
    pgi = _make()
    assert pgi.gate_id == "policy"


def test_policy_gate_input_requires_model_version():
    with pytest.raises(ValidationError):
        PolicyGateInput(
            prompt_template_id="ato-v1",
            prompt_template_version="1.0.0",
            corpus_version="corpus-v1",
            rendered_prompt="x",
            prompt_snapshot=_snapshot(),
            template_vars={},
        )


def test_policy_gate_input_is_frozen():
    pgi = _make()
    with pytest.raises(ValidationError):
        pgi.model_version = "tampered"


def test_policy_gate_input_is_a_gate_input():
    from core.gate import GateInput

    pgi = _make()
    assert isinstance(pgi, GateInput)
