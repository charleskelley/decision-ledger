"""Tests for PolicyGate helper functions — rendering and output parsing.

Per project guidelines, PolicyGate.evaluate() is not tested with mocked LLM
responses. Tests cover pure helpers only: prompt rendering, snippet
formatting, and JSON/schema parsing/validation.
"""

from __future__ import annotations

import json

from app.gate.policy.gate import (
    _parse_verdict,
    _render_prompt,
    _render_snippets,
)
from core.actions import DecisionAction
from core.snippet import RetrievedSnippet


def _make_snippet(
    document_id: str,
    text: str = "Sample policy text.",
    *,
    jurisdiction: str = "US_FEDERAL",
    section_path: str = "Section 1",
) -> RetrievedSnippet:
    return RetrievedSnippet(
        document_id=document_id,
        title=f"Policy {document_id}",
        version="1.0",
        jurisdiction=jurisdiction,
        section_path=section_path,
        text=text,
        relevance_score=0.8,
        retrieval_path="rrf_only",
    )


def _valid_gate_json(**overrides: object) -> str:
    data: dict[str, object] = {
        "permitted_actions": ["ALLOW"],
        "required_controls": [],
        "rationale": "Low risk profile. No anomalies detected.",
        "citations": [
            {
                "policy_id": "NIST-800-63B",
                "snippet": "Verifiers SHALL require MFA.",
                "relevance": "Supports MFA requirement at AAL2.",
            }
        ],
        "confidence": 0.88,
        "escalate_to_human": False,
        "escalation_reason": None,
    }
    data.update(overrides)
    return json.dumps(data)


_SIMPLE_TEMPLATE = "Risk: {risk_score}\nEvidence:\n{policy_snippets}"


# ---------------------------------------------------------------------------
# _render_snippets
# ---------------------------------------------------------------------------


def test_render_snippets_empty_returns_placeholder():
    result = _render_snippets([])
    assert "(no policy evidence retrieved)" in result


def test_render_snippets_numbers_entries():
    snippets = [_make_snippet("DOC-A"), _make_snippet("DOC-B")]
    result = _render_snippets(snippets)
    assert "[1]" in result
    assert "[2]" in result


def test_render_snippets_includes_jurisdiction_and_section():
    snippet = _make_snippet("DOC-A", section_path="5.2 — Authentication")
    result = _render_snippets([snippet])
    assert "US_FEDERAL" in result
    assert "5.2 — Authentication" in result


def test_render_snippets_includes_verbatim_text():
    snippet = _make_snippet("DOC-A", text="You MUST implement MFA at AAL2.")
    result = _render_snippets([snippet])
    assert "You MUST implement MFA at AAL2." in result


def test_render_snippets_single_snippet_has_no_extra_separator():
    result = _render_snippets([_make_snippet("DOC-A")])
    assert result.count("[1]") == 1
    assert "[2]" not in result


# ---------------------------------------------------------------------------
# _render_prompt
# ---------------------------------------------------------------------------


def test_render_prompt_substitutes_template_vars():
    result = _render_prompt(_SIMPLE_TEMPLATE, {"risk_score": "0.75"}, [])
    assert "Risk: 0.75" in result


def test_render_prompt_injects_snippet_text():
    snippet = _make_snippet("DOC-A", text="Require step-up MFA.")
    result = _render_prompt(_SIMPLE_TEMPLATE, {"risk_score": "0.10"}, [snippet])
    assert "Require step-up MFA." in result


def test_render_prompt_escapes_braces_in_snippet_text():
    # Snippet text containing { } must not break str.format()
    snippet = _make_snippet("DOC-A", text="Use {algorithm} with key length 256.")
    result = _render_prompt(_SIMPLE_TEMPLATE, {"risk_score": "0.50"}, [snippet])
    assert "{algorithm}" in result


def test_render_prompt_empty_snippets_uses_no_evidence_placeholder():
    result = _render_prompt(_SIMPLE_TEMPLATE, {"risk_score": "0.20"}, [])
    assert "(no policy evidence retrieved)" in result


def test_render_prompt_multiple_vars():
    template = "Score: {risk_score} | Method: {auth_method}\n{policy_snippets}"
    result = _render_prompt(
        template,
        {"risk_score": "0.30", "auth_method": "PASSWORD"},
        [],
    )
    assert "Score: 0.30" in result
    assert "Method: PASSWORD" in result


# ---------------------------------------------------------------------------
# _parse_verdict
# ---------------------------------------------------------------------------


def test_parse_verdict_valid_json_returns_output():
    result = _parse_verdict(_valid_gate_json(), decision_id="dec-001")
    assert result is not None
    assert result.permitted_actions == [DecisionAction.ALLOW]
    assert result.confidence == 0.88


def test_parse_verdict_invalid_json_returns_none():
    result = _parse_verdict("this is not json", decision_id="dec-001")
    assert result is None


def test_parse_verdict_empty_string_returns_none():
    result = _parse_verdict("", decision_id="dec-001")
    assert result is None


def test_parse_verdict_missing_required_field_returns_none():
    # Missing 'rationale' — schema validation should fail
    data = json.dumps(
        {
            "permitted_actions": ["ALLOW"],
            "required_controls": [],
            "citations": [],
            "confidence": 0.9,
            "escalate_to_human": False,
            "escalation_reason": None,
        }
    )
    result = _parse_verdict(data, decision_id="dec-001")
    assert result is None


def test_parse_verdict_extra_fields_are_ignored():
    # prompt_version and model_id are LLM-added fields absent from PolicyGateOutput
    result = _parse_verdict(
        _valid_gate_json(prompt_version="ato-v1", model_id="gpt-4o-2024-08-06"),
        decision_id="dec-001",
    )
    assert result is not None


def test_parse_verdict_multiple_permitted_actions():
    result = _parse_verdict(
        _valid_gate_json(permitted_actions=["ALLOW", "CHALLENGE"]),
        decision_id="dec-001",
    )
    assert result is not None
    assert DecisionAction.CHALLENGE in result.permitted_actions
    assert DecisionAction.ALLOW in result.permitted_actions


def test_parse_verdict_escalate_to_human_true():
    result = _parse_verdict(
        _valid_gate_json(
            escalate_to_human=True,
            escalation_reason="Jurisdiction conflict requires legal review.",
        ),
        decision_id="dec-001",
    )
    assert result is not None
    assert result.escalate_to_human is True
    assert result.escalation_reason is not None


def test_parse_verdict_confidence_out_of_range_returns_none():
    # confidence > 1.0 should fail Pydantic field validation (le=1.0)
    result = _parse_verdict(
        _valid_gate_json(confidence=1.5),
        decision_id="dec-001",
    )
    assert result is None
