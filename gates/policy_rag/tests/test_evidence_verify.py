"""The policy gate's evidence escrow and verify, over fake providers (DR-34 part 2).

Neither path calls the LLM, so neither needs a mocked model: the outputs
verified here are built the way ``evaluate`` builds them — retrieval over a
binding (``retrieve_evidence``), the escrow step (``escrow_documents``), the
snapshot, and ``_policy_gate_output`` — around a fixed verdict. The fake
provider holds whole documents (``FetchingProvider``, with spans) and the
escrow is a dict (``MemoryEscrow``). What is pinned:

- Escrow: each cited document is fetched once and escrowed whole; every
  passage from it cites the digest the escrow returned (none with escrow
  off, or from a provider that cannot fetch documents). A fetch that fails
  or returns another document fails the decision.
- Stored verify: a passage is ``intact`` when the escrowed document holds
  it at its span; ``source_changed`` when the escrowed document does not
  (it changed between retrieval and escrow); ``not_verifiable`` with
  nothing escrowed, the escrow lost, or a reference that does not match its
  passage, and for every reference of an older snapshot.
- Live verify: against the document as the provider holds it now —
  ``intact``, ``source_changed`` (edited, or its version gone),
  ``source_unavailable`` (corpus gone, fetch failing, provider not
  installed), ``not_verifiable`` (no ``DocumentFetcher``); each document is
  fetched once.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from core.actions import DecisionAction
from core.gate import EvidenceRef, GateOutput, GateVerdict
from policy_rag_gate.corpus import PolicyBindingConfig
from policy_rag_gate.gate import (
    EvidenceEscrowError,
    PolicyGate,
    _policy_gate_output,
    escrow_documents,
    passage_holds,
    retrieve_evidence,
)
from policy_rag_gate.models import (
    PolicyGateExtra,
    PolicyGateSnapshot,
    PromptSnapshot,
    content_hash,
)
from policy_rag_gate.providers import ProviderContractError, ProviderRegistry

if TYPE_CHECKING:
    from policy_rag_gate.models import RetrievedContext

_REG = "regulatory"
_NOTES = "practice-notes"


@pytest.fixture
def corpora(fake_passage) -> dict[str, Any]:
    """Two corpora; REG-1 has two passages, so its document is cited twice."""
    return {
        _REG: [
            fake_passage("REG-1", "Hold any account with a fresh device.", title="Reg"),
            fake_passage("REG-1", "Escalate when signals disagree.", title="Reg"),
        ],
        _NOTES: [
            fake_passage("PN-1", "Challenge a fresh device with step-up.", title="PN")
        ],
    }


@pytest.fixture
def provider(fetching_provider, corpora) -> Any:
    return fetching_provider(corpora)


def _binding() -> PolicyBindingConfig:
    return PolicyBindingConfig.model_validate(
        {
            "corpora": [
                {"corpus_id": _REG, "tier": 1},
                {"corpus_id": _NOTES, "tier": 2},
            ],
            "k_per_corpus": 5,
        }
    )


def _gate(providers: ProviderRegistry, escrow: Any) -> PolicyGate:
    # Verify never calls the LLM or renders a prompt.
    return PolicyGate(
        client=MagicMock(name="unused-llm"),
        prompt_registry=MagicMock(name="unused-prompts"),
        providers=providers,
        escrow=escrow,
    )


def _decided(
    providers: ProviderRegistry,
    escrow: Any,
    *,
    before_escrow: Any = None,
) -> GateOutput:
    """An output as ``evaluate`` builds it: retrieve, escrow, snapshot."""
    evidence = retrieve_evidence(providers, _binding(), "fresh device")
    if before_escrow is not None:
        before_escrow()
    contexts: list[RetrievedContext] = (
        escrow_documents(
            providers, evidence.queries, evidence.contexts, escrow, decision_id="d-1"
        )
        if escrow is not None
        else list(evidence.contexts)
    )
    snapshot = PolicyGateSnapshot(
        retrieval_query="fresh device",
        corpus_queries=evidence.queries,
        contexts=contexts,
        evidence_layout="tiered_blocks",
        dedup_merged=evidence.dedup_merged,
        index_version="test",
        model_version="unused",
        prompt_template_id="t",
        prompt_template_version="1",
        rendered_prompt="",
        prompt_snapshot=PromptSnapshot(
            template_id="t", version="1", template_text="", system_text=""
        ),
        template_vars={},
    )
    return _policy_gate_output(
        verdict=GateVerdict(
            gate_id="policy",
            permitted_actions=[DecisionAction.HOLD],
            required_controls=[],
            confidence=0.9,
            escalate_to_human=False,
            escalation_reason=None,
        ),
        contexts=contexts,
        snapshot=snapshot,
        extra=PolicyGateExtra(),
    )


def _verify(gate: PolicyGate, output: GateOutput, *, live: bool) -> tuple[str, ...]:
    return asyncio.run(gate.verify(output, live=live))


def _documents(output: GateOutput) -> list[str]:
    return [ref.source_id for ref in output.evidence]


# ---------------------------------------------------------------------------
# Escrow
# ---------------------------------------------------------------------------


def test_each_cited_document_is_escrowed_once_and_cited_by_its_digest(
    provider, memory_escrow
):
    output = _decided(ProviderRegistry.of(provider), memory_escrow)

    reg = provider.documents[(_REG, "REG-1", "1.0")]
    notes = provider.documents[(_NOTES, "PN-1", "1.0")]
    assert sorted(provider.fetched) == [(_NOTES, "PN-1", "1.0"), (_REG, "REG-1", "1.0")]
    assert memory_escrow.rows == {
        hashlib.sha256(reg.encode()).hexdigest(): reg.encode(),
        hashlib.sha256(notes.encode()).hexdigest(): notes.encode(),
    }
    assert memory_escrow.puts == [("text/markdown", "d-1", "policy")] * 2
    assert _documents(output) == ["REG-1", "REG-1", "PN-1"]
    assert [ref.artifact_hash for ref in output.evidence] == [
        content_hash(reg),
        content_hash(reg),
        content_hash(notes),
    ]
    # The reference's content digest stays the passage's.
    assert output.evidence[0].content_hash == content_hash(
        "Hold any account with a fresh device."
    )


def test_with_escrow_off_passages_cite_no_artifact(provider, memory_escrow):
    escrow_off = type(memory_escrow)(enabled=False)

    output = _decided(ProviderRegistry.of(provider), escrow_off)

    assert [ref.artifact_hash for ref in output.evidence] == [None] * 3
    assert escrow_off.rows == {}
    # Nothing would be kept, so nothing is fetched.
    assert provider.fetched == []
    assert escrow_off.puts == []


def test_a_provider_that_cannot_fetch_documents_escrows_nothing(
    fake_provider, corpora, memory_escrow
):
    output = _decided(ProviderRegistry.of(fake_provider(corpora)), memory_escrow)

    assert [ref.artifact_hash for ref in output.evidence] == [None] * 3
    assert memory_escrow.rows == {}


def test_a_fetch_that_fails_fails_the_decision(
    fetching_provider, corpora, memory_escrow
):
    provider = fetching_provider(corpora, fetch_error=RuntimeError("store down"))

    with pytest.raises(RuntimeError, match="store down"):
        _decided(ProviderRegistry.of(provider), memory_escrow)


def test_a_fetched_document_that_is_not_the_one_asked_breaks_the_contract(
    provider, memory_escrow, monkeypatch
):
    fetch = provider.fetch_document

    def _another(corpus, document_id, version):
        return fetch(corpus, "PN-1" if document_id == "REG-1" else document_id, version)

    monkeypatch.setattr(provider, "fetch_document", _another)
    provider.documents[(_REG, "PN-1", "1.0")] = "# PN\n"
    # REG-1 is asked for under the regulatory corpus; the provider answers
    # with a document of another id.
    with pytest.raises(ProviderContractError, match="REG-1"):
        _decided(ProviderRegistry.of(provider), memory_escrow)


# ---------------------------------------------------------------------------
# Verify, stored
# ---------------------------------------------------------------------------


def test_stored_verify_proves_every_passage_from_its_escrowed_document(
    provider, memory_escrow
):
    providers = ProviderRegistry.of(provider)
    output = _decided(providers, memory_escrow)
    provider.fetched.clear()

    assert _verify(_gate(providers, memory_escrow), output, live=False) == (
        "intact",
        "intact",
        "intact",
    )
    assert provider.fetched == []  # the escrow alone answers


def test_stored_verify_holds_after_the_source_changes_or_disappears(
    provider, memory_escrow
):
    providers = ProviderRegistry.of(provider)
    output = _decided(providers, memory_escrow)
    provider.documents[(_REG, "REG-1", "1.0")] = "# Reg\n\nRewritten.\n"
    provider.drop_corpus(_NOTES)

    assert _verify(_gate(providers, memory_escrow), output, live=False) == (
        "intact",
        "intact",
        "intact",
    )


def test_a_document_that_changed_before_it_was_escrowed_fails_the_decision(
    provider, memory_escrow
):
    providers = ProviderRegistry.of(provider)

    def _edit() -> None:  # between retrieval and escrow (a reload racing)
        provider.documents[(_REG, "REG-1", "1.0")] = "# Reg\n\nAnother text.\n"

    with pytest.raises(EvidenceEscrowError, match="ranked \\[1, 2\\]"):
        _decided(providers, memory_escrow, before_escrow=_edit)
    # Nothing of that document was kept for a decision it cannot prove.
    assert all(b"Another text" not in row for row in memory_escrow.rows.values())


def test_an_escrowed_document_that_does_not_hold_its_passage_verifies_changed(
    provider, memory_escrow
):
    # Escrow refuses this at decision time; a reference pointing at such an
    # artifact anyway (a hand-made or altered record) is reported, not trusted.
    providers = ProviderRegistry.of(provider)
    output = _decided(providers, memory_escrow)
    other = memory_escrow.put(
        b"# Reg\n\nAnother text.\n",
        media_type="text/markdown",
        decision_id="d-x",
        gate_id="policy",
    )
    first, *rest = output.evidence
    altered = output.model_copy(
        update={"evidence": (first.model_copy(update={"artifact_hash": other}), *rest)}
    )

    assert _verify(_gate(providers, memory_escrow), altered, live=False) == (
        "source_changed",
        "intact",
        "intact",
    )


def test_without_an_escrowed_artifact_stored_verify_cannot_prove_anything(
    provider, memory_escrow
):
    providers = ProviderRegistry.of(provider)
    not_escrowed = _decided(providers, None)
    escrowed = _decided(providers, memory_escrow)
    lost = memory_escrow.rows.pop(escrowed.evidence[2].artifact_hash or "")
    assert lost

    gate = _gate(providers, memory_escrow)

    assert _verify(gate, not_escrowed, live=False) == ("not_verifiable",) * 3
    assert _verify(gate, escrowed, live=False) == (
        "intact",
        "intact",
        "not_verifiable",
    )
    # A gate built without the escrow cannot read it either.
    assert _verify(_gate(providers, None), escrowed, live=False) == (
        ("not_verifiable",) * 3
    )


def test_an_escrowed_artifact_that_is_not_what_was_kept_is_not_verifiable(
    provider, memory_escrow
):
    providers = ProviderRegistry.of(provider)
    output = _decided(providers, memory_escrow)
    digest = output.evidence[2].artifact_hash or ""
    memory_escrow.rows[digest] = b"tampered bytes"

    assert _verify(_gate(providers, memory_escrow), output, live=False)[2] == (
        "not_verifiable"
    )


def test_a_reference_that_does_not_match_its_passage_is_not_verifiable(
    provider, memory_escrow
):
    providers = ProviderRegistry.of(provider)
    output = _decided(providers, memory_escrow)
    first, *rest = output.evidence
    forged = output.model_copy(
        update={
            "evidence": (
                first.model_copy(update={"content_hash": content_hash("other")}),
                *rest,
                EvidenceRef(source_id="EXTRA", content_hash=content_hash("x")),
            )
        }
    )

    assert _verify(_gate(providers, memory_escrow), forged, live=False) == (
        "not_verifiable",
        "intact",
        "intact",
        "not_verifiable",
    )


@pytest.mark.parametrize("snapshot", [None, {"retrieval_results": []}, {"x": 1}])
def test_an_older_or_foreign_snapshot_is_not_verifiable(
    provider, memory_escrow, snapshot
):
    providers = ProviderRegistry.of(provider)
    output = _decided(providers, memory_escrow).model_copy(
        update={"evidence_snapshot": snapshot}
    )

    for live in (False, True):
        assert _verify(_gate(providers, memory_escrow), output, live=live) == (
            ("not_verifiable",) * 3
        )


# ---------------------------------------------------------------------------
# Verify, live
# ---------------------------------------------------------------------------


def test_live_verify_reads_each_document_once_and_finds_it_unchanged(
    provider, memory_escrow
):
    providers = ProviderRegistry.of(provider)
    output = _decided(providers, memory_escrow)
    provider.fetched.clear()

    assert _verify(_gate(providers, memory_escrow), output, live=True) == (
        "intact",
        "intact",
        "intact",
    )
    assert sorted(provider.fetched) == [(_NOTES, "PN-1", "1.0"), (_REG, "REG-1", "1.0")]


def test_a_document_edited_since_reads_changed_live_and_intact_from_escrow(
    provider, memory_escrow
):
    providers = ProviderRegistry.of(provider)
    output = _decided(providers, memory_escrow)
    key = (_REG, "REG-1", "1.0")
    # A sentence inserted before the passages: every span in it moves.
    provider.documents[key] = provider.documents[key].replace(
        "# Reg\n\n", "# Reg\n\nA new preamble.\n\n"
    )
    gate = _gate(providers, memory_escrow)

    assert _verify(gate, output, live=True) == (
        "source_changed",
        "source_changed",
        "intact",
    )
    assert _verify(gate, output, live=False) == ("intact",) * 3


def test_a_version_no_longer_held_while_its_corpus_is_reads_changed(
    provider, memory_escrow
):
    providers = ProviderRegistry.of(provider)
    output = _decided(providers, memory_escrow)
    del provider.documents[(_REG, "REG-1", "1.0")]  # re-versioned and reloaded

    assert _verify(_gate(providers, memory_escrow), output, live=True) == (
        "source_changed",
        "source_changed",
        "intact",
    )


def test_a_corpus_no_longer_held_reads_unavailable_live_and_intact_from_escrow(
    provider, memory_escrow
):
    providers = ProviderRegistry.of(provider)
    output = _decided(providers, memory_escrow)
    provider.drop_corpus(_REG)
    gate = _gate(providers, memory_escrow)

    assert _verify(gate, output, live=True) == (
        "source_unavailable",
        "source_unavailable",
        "intact",
    )
    assert _verify(gate, output, live=False) == ("intact",) * 3


def test_a_provider_that_fails_reads_unavailable(provider, memory_escrow):
    providers = ProviderRegistry.of(provider)
    output = _decided(providers, memory_escrow)
    provider.fetch_error = ConnectionError("store unreachable")

    assert _verify(_gate(providers, memory_escrow), output, live=True) == (
        ("source_unavailable",) * 3
    )


def test_a_provider_not_installed_reads_unavailable(provider, memory_escrow):
    output = _decided(ProviderRegistry.of(provider), memory_escrow)

    assert _verify(_gate(ProviderRegistry.of(), memory_escrow), output, live=True) == (
        ("source_unavailable",) * 3
    )


def test_a_provider_that_cannot_fetch_documents_is_not_verifiable_live(
    fake_provider, provider, corpora, memory_escrow
):
    output = _decided(ProviderRegistry.of(provider), memory_escrow)
    without = ProviderRegistry.of(fake_provider(corpora))

    assert _verify(_gate(without, memory_escrow), output, live=True) == (
        ("not_verifiable",) * 3
    )


def test_a_document_answering_for_another_reads_unavailable(
    provider, memory_escrow, monkeypatch
):
    providers = ProviderRegistry.of(provider)
    output = _decided(providers, memory_escrow)
    fetch = provider.fetch_document
    monkeypatch.setattr(
        provider,
        "fetch_document",
        lambda corpus, _doc, version: fetch(corpus, "X", version),
    )
    provider.documents[(_REG, "X", "1.0")] = "# X\n"
    provider.documents[(_NOTES, "X", "1.0")] = "# X\n"

    assert _verify(_gate(providers, memory_escrow), output, live=True) == (
        ("source_unavailable",) * 3
    )


# ---------------------------------------------------------------------------
# passage_holds
# ---------------------------------------------------------------------------


def test_a_passage_without_a_span_holds_when_its_text_occurs(retrieved_context):
    merged = retrieved_context.model_copy(update={"span": None})

    assert passage_holds(f"# Doc\n\n{merged.text}\n", merged)
    assert not passage_holds("# Doc\n\nSomething else.\n", merged)


def test_a_span_past_the_end_of_the_text_does_not_hold(retrieved_context):
    spanned = retrieved_context.model_copy(update={"span": (0, 10_000)})

    assert not passage_holds(retrieved_context.text, spanned)


def test_a_merged_passage_holds_when_its_paragraphs_occur_in_order(retrieved_context):
    # The loader merges a short section into the next and drops the heading
    # between them: the passage is not contiguous in its document.
    merged = retrieved_context.model_copy(
        update={"span": None, "text": "Short scope.\n\nThe next section's text."}
    )
    document = (
        "# Doc\n\n## 1. Scope\n\nShort scope.\n\n## 2. Next\n\n"
        "The next section's text.\n"
    )

    assert passage_holds(document, merged)
    assert not passage_holds(document.replace("Short scope.", "Edited."), merged)
    assert not passage_holds(
        "# Doc\n\nThe next section's text.\n\nShort scope.\n", merged
    )
