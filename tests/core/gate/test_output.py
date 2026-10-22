"""Behavioral tests for the universal GateVerdict, GateOutput, and EvidenceRef.

GateVerdict is the framework's enforcement-consumable verdict — universal
across gates. GateOutput is the open output contract (DR-28): a typed
universal base (gate_id, deterministic, verdict, evidence) plus an opaque,
gate-owned extension (evidence_snapshot, extra) that must survive the JSON
round trip the bundle store performs, unchanged.
"""

import hashlib
import json
import typing

import pytest
from pydantic import ValidationError

from core.actions import DecisionAction
from core.gate import EvidenceRef, EvidenceStatus, GateOutput, GateVerdict


def _make_verdict(**overrides) -> GateVerdict:
    base = {
        "gate_id": "policy",
        "permitted_actions": [DecisionAction.ALLOW],
        "required_controls": [],
        "confidence": 0.85,
        "escalate_to_human": False,
        "escalation_reason": None,
    }
    base.update(overrides)
    return GateVerdict(**base)


def test_gate_verdict_minimal_construction():
    verdict = _make_verdict()
    assert verdict.gate_id == "policy"
    assert verdict.permitted_actions == [DecisionAction.ALLOW]


def test_gate_verdict_rejects_confidence_above_one():
    with pytest.raises(ValidationError):
        _make_verdict(confidence=1.1)


def test_gate_verdict_rejects_confidence_below_zero():
    with pytest.raises(ValidationError):
        _make_verdict(confidence=-0.1)


def test_gate_verdict_accepts_multiple_permitted_actions():
    verdict = _make_verdict(
        permitted_actions=[DecisionAction.ALLOW, DecisionAction.CHALLENGE],
        required_controls=["step_up_mfa"],
        confidence=0.75,
    )
    assert len(verdict.permitted_actions) == 2


def test_gate_verdict_with_escalation_flag_and_reason():
    verdict = _make_verdict(
        permitted_actions=[DecisionAction.HOLD],
        confidence=0.45,
        escalate_to_human=True,
        escalation_reason="Conflicting jurisdiction signals.",
    )
    assert verdict.escalate_to_human
    assert verdict.escalation_reason is not None


def test_gate_verdict_is_immutable():
    verdict = _make_verdict()
    with pytest.raises(ValidationError):
        verdict.confidence = 0.5


# ---------------------------------------------------------------------------
# GateOutput — open contract: universal base + opaque extension
# ---------------------------------------------------------------------------


def _output(**overrides) -> GateOutput:
    base = {"gate_id": "policy", "deterministic": False, "verdict": _make_verdict()}
    base.update(overrides)
    return GateOutput(**base)


def test_gate_output_with_verdict():
    verdict = _make_verdict()
    output = _output(verdict=verdict)
    assert output.verdict is verdict
    assert output.gate_id == "policy"


def test_gate_output_verdict_may_be_none():
    # The no-valid-verdict shape enforcement routes to HOLD via Tier 1.
    output = _output(verdict=None)
    assert output.verdict is None


def test_gate_output_defaults_to_no_evidence_and_an_empty_extension():
    output = _output()
    assert output.evidence == ()
    assert output.evidence_snapshot is None
    assert output.extra == {}


def test_gate_output_requires_the_deterministic_flag():
    with pytest.raises(ValidationError, match="deterministic"):
        GateOutput(gate_id="policy", verdict=None)  # type: ignore[call-arg]


def test_gate_output_rejects_a_verdict_attributed_to_another_gate():
    with pytest.raises(ValidationError, match="does not match"):
        _output(gate_id="rules", verdict=_make_verdict(gate_id="policy"))


def test_gate_output_accepts_any_json_in_the_opaque_extension():
    output = _output(
        evidence_snapshot={"query": "q", "results": [{"score": 0.5}, None, True]},
        extra={"token_cost": {"usd": 0.01}, "notes": ["a", 1, 2.5]},
    )
    assert output.evidence_snapshot == {
        "query": "q",
        "results": [{"score": 0.5}, None, True],
    }
    assert output.extra["notes"] == ["a", 1, 2.5]


def test_gate_output_rejects_a_non_json_extension_value():
    with pytest.raises(ValidationError):
        _output(extra={"bad": object()})


def test_gate_output_survives_the_json_round_trip_unchanged():
    # The bundle store persists model_dump(mode="json") and reloads with
    # strict=False; tuples, frozensets, enums, and the opaque JSON must all
    # come back equal.
    output = _output(
        deterministic=True,
        evidence=(
            EvidenceRef(
                source_id="DOC-A", locator="2.1", jurisdictions=frozenset({"A"})
            ),
            EvidenceRef(source_id="DOC-B"),
        ),
        evidence_snapshot={"retrieval": {"path": "reranked", "k": 5}},
        extra={"rationale": "ok", "citations": []},
    )
    stored = json.loads(json.dumps(output.model_dump(mode="json")))
    assert GateOutput.model_validate(stored, strict=False) == output


def test_gate_output_evidence_jurisdictions_is_the_union_across_references():
    output = _output(
        evidence=(
            EvidenceRef(source_id="a", jurisdictions=frozenset({"A", "B"})),
            EvidenceRef(source_id="b", jurisdictions=frozenset({"B", "C"})),
            EvidenceRef(source_id="c"),
        )
    )
    assert output.evidence_jurisdictions() == frozenset({"A", "B", "C"})


def test_gate_output_without_evidence_has_no_jurisdictions():
    assert _output().evidence_jurisdictions() == frozenset()


def test_gate_output_is_immutable():
    output = _output(verdict=None)
    with pytest.raises(ValidationError):
        output.verdict = _make_verdict()


# ---------------------------------------------------------------------------
# EvidenceRef
# ---------------------------------------------------------------------------


def test_evidence_ref_defaults_to_no_locator_no_jurisdictions_and_no_digest():
    ref = EvidenceRef(source_id="ruleset@1.0")
    assert ref.locator is None
    assert ref.jurisdictions == frozenset()
    assert ref.content_hash is None
    assert ref.artifact_hash is None


def test_evidence_ref_carries_the_digest_of_the_text_the_gate_relied_on():
    digest = hashlib.sha256(b"Section 3.2 applies.").hexdigest()
    ref = EvidenceRef(source_id="DOC-A", locator="3.2", content_hash=digest)
    assert ref.content_hash == digest


def test_evidence_ref_carries_the_digest_of_its_escrowed_artifact():
    text = hashlib.sha256(b"Section 3.2 applies.").hexdigest()
    artifact = hashlib.sha256(b"# Policy A\n\nSection 3.2 applies.").hexdigest()
    ref = EvidenceRef(source_id="DOC-A", content_hash=text, artifact_hash=artifact)
    assert (ref.content_hash, ref.artifact_hash) == (text, artifact)


_NOT_SHA256_HEX = [
    "",
    "ABCDEF" + "0" * 58,  # upper case
    "0" * 63,  # one character short
    "0" * 65,
    "sha256:" + "0" * 64,  # a prefixed notation
    "g" * 64,
]


@pytest.mark.parametrize("field", ["content_hash", "artifact_hash"])
@pytest.mark.parametrize("digest", _NOT_SHA256_HEX)
def test_evidence_ref_refuses_a_digest_that_is_not_sha256_hex(field: str, digest: str):
    with pytest.raises(ValidationError):
        EvidenceRef(source_id="DOC-A", **{field: digest})


@pytest.mark.parametrize("field", ["content_hash", "artifact_hash"])
def test_evidence_ref_digest_is_strictly_a_string(field: str):
    with pytest.raises(ValidationError):
        EvidenceRef(source_id="DOC-A", **{field: b"0" * 64})


def test_a_stored_evidence_ref_without_a_digest_still_loads():
    # Bundles written before DR-34 carry no content_hash key; replay reloads
    # them with the current model.
    stored = {"source_id": "DOC-A", "locator": "2.1", "jurisdictions": ["A"]}
    ref = EvidenceRef.model_validate(stored, strict=False)
    assert ref.content_hash is None
    assert ref.artifact_hash is None
    assert ref == EvidenceRef(
        source_id="DOC-A", locator="2.1", jurisdictions=frozenset({"A"})
    )


def test_evidence_ref_is_immutable():
    ref = EvidenceRef(source_id="DOC-A")
    with pytest.raises(ValidationError):
        ref.source_id = "tampered"


def test_evidence_status_names_the_four_ways_a_reference_can_stand():
    assert typing.get_args(EvidenceStatus) == (
        "intact",
        "source_changed",
        "source_unavailable",
        "not_verifiable",
    )
