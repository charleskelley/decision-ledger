"""Tests for PolicyGate helper functions — rendering and snippet formatting.

Per project guidelines, PolicyGate.evaluate() is not tested with mocked LLM
responses. Tests cover pure helpers only: prompt rendering and snippet
formatting. Strict structured-output validation now happens at the
LLMClient SDK boundary (DR-23); the gate's prior _parse_verdict path
was removed when the SDK began returning Pydantic-validated instances
directly.
"""

from __future__ import annotations

from app.gate.policy.gate import (
    _render_prompt,
    _render_snippets,
)
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
