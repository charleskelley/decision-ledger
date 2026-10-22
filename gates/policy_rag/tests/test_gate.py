"""Tests for PolicyGate's pure helpers — rendering, evidence, and the tiered hold.

Per project guidelines, PolicyGate.evaluate() is not tested with mocked LLM
responses. Tests cover pure helpers only: how retrieved passages are
rendered into ``{policy_snippets}`` in each layout (the ``numbered_list``
rendering byte for byte as before DR-34), how a prompt is rendered, the
evidence references the gate reports, and the hold the gate applies to a
tiered verdict that reports disagreeing or insufficient evidence. Strict
structured-output validation happens at the LLMClient SDK boundary (DR-23).
"""

from __future__ import annotations

from typing import Any

import pytest

from core.actions import DecisionAction
from core.gate import GateVerdict
from policy_rag_gate.gate import (
    NO_EVIDENCE,
    _evidence,
    _held,
    _render_prompt,
    render_evidence,
    render_numbered_list,
    render_tiered_blocks,
)
from policy_rag_gate.models import (
    Citation,
    RetrievedContext,
    TieredPolicyGateVerdict,
    content_hash,
)


def _context(
    document_id: str,
    text: str = "Sample policy text.",
    *,
    corpus_id: str = "corpus-a",
    tier: int = 1,
    rank: int = 1,
    jurisdiction: str | None = "US_FEDERAL",
    section_path: str | None = "Section 1",
    version: str | None = "1.0",
) -> RetrievedContext:
    return RetrievedContext(
        corpus_id=corpus_id,
        provider="reference",
        tier=tier,
        document_id=document_id,
        title=f"Policy {document_id}",
        version=version,
        jurisdiction=jurisdiction,
        section_path=section_path,
        text=text,
        content_hash=content_hash(text),
        rank=rank,
        provenance_level="pinned_version",
    )


def _pre_dr34_rendering(contexts: list[RetrievedContext]) -> str:
    """The gate's rendering before DR-34, verbatim (``_render_snippets``)."""
    if not contexts:
        return "(no policy evidence retrieved)"
    parts = []
    for i, s in enumerate(contexts, 1):
        parts.append(
            f"[{i}] {s.title} (v{s.version}) — {s.jurisdiction}\n"
            f"Section: {s.section_path}\n"
            f"{s.text}"
        )
    return "\n\n".join(parts)


_SIMPLE_TEMPLATE = "Risk: {risk_score}\nEvidence:\n{policy_snippets}"


# ---------------------------------------------------------------------------
# numbered_list — every template written before DR-34
# ---------------------------------------------------------------------------


def test_the_numbered_list_is_byte_for_byte_the_rendering_before_dr34():
    contexts = [
        _context("DOC-A", "You MUST implement MFA at AAL2.", section_path="5.2 — Auth"),
        _context("DOC-B", "Block impossible travel.", jurisdiction="INTERNAL", rank=2),
    ]

    assert render_numbered_list(contexts) == _pre_dr34_rendering(contexts)


def test_the_numbered_list_without_evidence_is_the_placeholder():
    assert render_numbered_list([]) == NO_EVIDENCE == _pre_dr34_rendering([])


def test_the_numbered_list_leaves_out_what_a_provider_does_not_supply():
    rendered = render_numbered_list(
        [_context("DOC-A", "text", jurisdiction=None, section_path=None, version=None)]
    )

    assert rendered == "[1] Policy DOC-A\ntext"


def test_the_numbered_list_numbers_entries_once():
    rendered = render_numbered_list([_context("DOC-A")])

    assert rendered.count("[1]") == 1
    assert "[2]" not in rendered


# ---------------------------------------------------------------------------
# tiered_blocks — templates with the conflict/gap protocol (DR-34)
# ---------------------------------------------------------------------------


def test_tiered_blocks_group_passages_by_tier_highest_first_with_labels():
    contexts = [
        _context("REG-1", "Hold it.", corpus_id="regulatory", tier=1),
        _context("PN-1", "Challenge it.", corpus_id="practice", tier=2),
        _context("PN-2", "Or step up.", corpus_id="practice", tier=2, rank=2),
    ]

    rendered = render_tiered_blocks(contexts)

    assert rendered == (
        "--- Tier 1 (corpora: regulatory) ---\n\n"
        "[1] Policy REG-1 — US_FEDERAL\n"
        "Corpus: regulatory | Tier: 1 | Document: REG-1 | Version: 1.0\n"
        "Section: Section 1\n"
        "Hold it.\n\n"
        "--- Tier 2 (corpora: practice) ---\n\n"
        "[2] Policy PN-1 — US_FEDERAL\n"
        "Corpus: practice | Tier: 2 | Document: PN-1 | Version: 1.0\n"
        "Section: Section 1\n"
        "Challenge it.\n\n"
        "[3] Policy PN-2 — US_FEDERAL\n"
        "Corpus: practice | Tier: 2 | Document: PN-2 | Version: 1.0\n"
        "Section: Section 1\n"
        "Or step up."
    )


def test_a_tier_block_names_every_corpus_in_it_in_order():
    rendered = render_tiered_blocks(
        [
            _context("B-1", "b", corpus_id="b", tier=1),
            _context("A-1", "a", corpus_id="a", tier=1),
        ]
    )

    assert rendered.startswith("--- Tier 1 (corpora: b, a) ---")


def test_tiered_blocks_without_evidence_are_the_placeholder():
    assert render_tiered_blocks([]) == NO_EVIDENCE


@pytest.mark.parametrize(
    ("layout", "renderer"),
    [("numbered_list", render_numbered_list), ("tiered_blocks", render_tiered_blocks)],
)
def test_the_templates_layout_picks_the_rendering(layout, renderer):
    contexts = [_context("DOC-A")]

    assert render_evidence(contexts, layout) == renderer(contexts)


# ---------------------------------------------------------------------------
# _render_prompt
# ---------------------------------------------------------------------------


def test_render_prompt_substitutes_template_vars():
    result = _render_prompt(_SIMPLE_TEMPLATE, {"risk_score": "0.75"}, NO_EVIDENCE)
    assert "Risk: 0.75" in result


def test_render_prompt_injects_the_rendered_evidence():
    evidence = render_numbered_list([_context("DOC-A", "Require step-up MFA.")])
    result = _render_prompt(_SIMPLE_TEMPLATE, {"risk_score": "0.10"}, evidence)
    assert "Require step-up MFA." in result


def test_render_prompt_escapes_braces_in_passage_text():
    # Passage text containing { } must not break str.format()
    evidence = render_numbered_list(
        [_context("DOC-A", "Use {algorithm} with key length 256.")]
    )
    result = _render_prompt(_SIMPLE_TEMPLATE, {"risk_score": "0.50"}, evidence)
    assert "{algorithm}" in result


def test_render_prompt_without_evidence_uses_the_placeholder():
    result = _render_prompt(_SIMPLE_TEMPLATE, {"risk_score": "0.20"}, NO_EVIDENCE)
    assert "(no policy evidence retrieved)" in result


def test_render_prompt_multiple_vars():
    template = "Score: {risk_score} | Method: {auth_method}\n{policy_snippets}"
    result = _render_prompt(
        template, {"risk_score": "0.30", "auth_method": "PASSWORD"}, NO_EVIDENCE
    )
    assert "Score: 0.30" in result
    assert "Method: PASSWORD" in result


def test_render_prompt_raises_on_a_missing_variable():
    with pytest.raises(KeyError):
        _render_prompt(_SIMPLE_TEMPLATE, {}, NO_EVIDENCE)


# ---------------------------------------------------------------------------
# Evidence references
# ---------------------------------------------------------------------------


def test_each_rendered_passage_becomes_an_evidence_reference_with_its_digest():
    contexts = [
        _context("DOC-A", "a text", jurisdiction="REGION_A"),
        _context("DOC-B", "b text", jurisdiction=None, section_path=None, rank=2),
    ]

    refs = _evidence(contexts)

    assert [(r.source_id, r.locator, r.jurisdictions) for r in refs] == [
        ("DOC-A", "Section 1", frozenset({"REGION_A"})),
        ("DOC-B", None, frozenset()),
    ]
    assert [r.content_hash for r in refs] == [
        content_hash("a text"),
        content_hash("b text"),
    ]


# ---------------------------------------------------------------------------
# The hold on a tiered verdict (pure; the LLM call itself is eval-tested)
# ---------------------------------------------------------------------------


def _tiered(**overrides: Any) -> TieredPolicyGateVerdict:
    fields: dict[str, Any] = {
        "permitted_actions": [DecisionAction.ALLOW, DecisionAction.CHALLENGE],
        "required_controls": ["log_step_up_reason"],
        "rationale": "Tier 1 governs.",
        "citations": [Citation(policy_id="REG-1", snippet="Hold it.", relevance="r")],
        "confidence": 0.9,
        "escalate_to_human": False,
        "escalation_reason": None,
        "governing_corpus_id": "regulatory",
        "conflict_detected": False,
        "evidence_sufficient": True,
    }
    fields.update(overrides)
    return TieredPolicyGateVerdict(**fields)


def test_a_tiered_verdict_with_sufficient_agreeing_evidence_is_kept():
    verdict = _tiered()

    held, hold = _held(verdict)

    assert hold is None
    assert held == verdict.universal()
    assert type(held) is GateVerdict


def test_disagreeing_evidence_is_held_for_review():
    verdict = _tiered(conflict_detected=True)

    held, hold = _held(verdict)

    assert hold == "conflict_detected"
    assert held.permitted_actions == [DecisionAction.HOLD]
    assert held.escalate_to_human is True
    assert held.escalation_reason is not None
    # The model's controls and confidence stay; only the action is held.
    assert held.required_controls == ["log_step_up_reason"]
    assert held.confidence == 0.9


@pytest.mark.parametrize(
    ("permitted", "held_as"),
    [
        ([DecisionAction.ALLOW], [DecisionAction.HOLD]),
        ([DecisionAction.CHALLENGE, DecisionAction.HOLD], [DecisionAction.HOLD]),
        ([DecisionAction.BLOCK], [DecisionAction.BLOCK]),
        (
            [DecisionAction.HOLD, DecisionAction.BLOCK],
            [DecisionAction.HOLD, DecisionAction.BLOCK],
        ),
    ],
)
@pytest.mark.parametrize(
    "overrides", [{"conflict_detected": True}, {"evidence_sufficient": False}]
)
def test_a_hold_floors_the_verdict_at_hold_and_never_relaxes_it(
    permitted, held_as, overrides
):
    # Enforcement takes the most conservative permitted action, so a model
    # BLOCK must survive the hold: holding never makes a decision milder.
    held, _ = _held(_tiered(permitted_actions=permitted, **overrides))

    assert held.permitted_actions == held_as
    assert held.escalate_to_human is True


def test_insufficient_evidence_is_held_for_review():
    held, hold = _held(_tiered(evidence_sufficient=False, governing_corpus_id=None))

    assert hold == "evidence_insufficient"
    assert held.permitted_actions == [DecisionAction.HOLD]
    assert held.escalate_to_human is True


def test_disagreement_outranks_insufficiency_as_the_reason():
    _, hold = _held(_tiered(conflict_detected=True, evidence_sufficient=False))

    assert hold == "conflict_detected"


@pytest.mark.parametrize(
    "overrides", [{"conflict_detected": True}, {"evidence_sufficient": False}]
)
@pytest.mark.parametrize(
    "governing", ["regulatory", "eu-jurisdiction-rules", "conflict-of-interest", None]
)
def test_the_holds_reason_does_not_read_as_a_jurisdiction_conflict(
    overrides, governing
):
    # Enforcement's Tier 5 reads "conflict" or "jurisdiction" in an
    # escalation as a jurisdiction conflict; the gate's own hold is not one,
    # and the reason carries nothing the model wrote (whatever corpus it
    # named governing).
    held, _ = _held(_tiered(governing_corpus_id=governing, **overrides))

    reason = (held.escalation_reason or "").lower()
    assert reason
    assert "conflict" not in reason
    assert "jurisdiction" not in reason
    assert governing is None or governing not in reason


def test_a_held_block_is_enforced_as_a_block(fixture_reasoner):
    from app.enforcement.resolver import resolve_gate_output
    from core.gate import EvidenceRef, GateOutput

    held, _ = _held(
        _tiered(
            conflict_detected=True,
            permitted_actions=[DecisionAction.BLOCK],
            governing_corpus_id="eu-jurisdiction-rules",
        )
    )
    output = GateOutput(
        gate_id="policy",
        deterministic=False,
        verdict=held,
        evidence=(
            EvidenceRef(
                source_id="REG-1",
                locator="1. Scope",
                jurisdictions=frozenset({"REGION_A"}),
            ),
        ),
    )

    enforced = resolve_gate_output(
        output, registration=fixture_reasoner.registration, decision_id="d-1"
    )

    assert enforced.decision_action == DecisionAction.BLOCK
    assert enforced.enforcement_rule_applied is None
