"""``POST /api/v1/decisions/{decision_id}/verify`` (examples: gate, record).

Verify says how the evidence a decision relied on stands now: one entry per
evidence reference of each gate that ran, with two independent answers —
``escrow`` (does the instance's evidence escrow hold the artifact the
reference names: ``present``, ``missing``, or ``none``) and ``status`` (the
gate's own check: ``intact``, ``source_changed``, ``source_unavailable``, or
``not_verifiable``) — and a summary that counts them. The contract only is
checked: what a gate cites, and whether it ran at all (a gate that failed
cites nothing), is the deployment's. Verify changes nothing: the bundle reads
the same afterwards. ``?live=true`` is accepted; another reasoner's key and
an unknown id read as not found.
"""

from __future__ import annotations

from typing import Any

from conformance._ledger import Ledger, example, problem

_ESCROW = {"present", "missing", "none"}
_STATUSES = {"intact", "source_changed", "source_unavailable", "not_verifiable"}
_REF_FIELDS = {
    "gate_id",
    "source_id",
    "locator",
    "content_hash",
    "artifact_hash",
    "escrow",
    "status",
}


def _verify(
    ledger: Ledger, decision_id: str, key: str, *, live: bool | None = None
) -> Any:
    query = "" if live is None else f"?live={'true' if live else 'false'}"
    return ledger.post(f"/api/v1/decisions/{decision_id}/verify{query}", b"", key=key)


def _check_contract(result: dict[str, Any], *, decision_id: str, live: bool) -> None:
    assert result["decision_id"] == decision_id
    assert result["live"] is live
    assert result["checked_at"]
    refs = result["refs"]
    for ref in refs:
        assert not _REF_FIELDS - set(ref), ref
        assert ref["escrow"] in _ESCROW
        assert ref["status"] in _STATUSES
        if ref["artifact_hash"] is None:
            assert ref["escrow"] == "none"
        else:
            # A fresh decision's escrowed artifact is held.
            assert ref["escrow"] == "present"
    summary = result["summary"]
    assert summary["intact"] + summary["changed"] + summary["unavailable"] + summary[
        "not_verifiable"
    ] == len(refs)
    assert summary["intact"] == sum(r["status"] == "intact" for r in refs)
    assert summary["changed"] == sum(r["status"] == "source_changed" for r in refs)
    assert summary["unavailable"] == sum(
        r["status"] == "source_unavailable" for r in refs
    )
    assert summary["escrow_missing"] == sum(r["escrow"] == "missing" for r in refs)


def test_a_gated_decisions_evidence_is_verified(ledger: Ledger, key: str):
    receipt = ledger.submit(example("gate.json")).json()
    decision_id = receipt["decision_id"]

    response = _verify(ledger, decision_id, key)

    assert response.status_code == 200, response.text
    result = response.json()
    _check_contract(result, decision_id=decision_id, live=False)
    ran = {g["gate_id"] for g in receipt["gates"] if g["status"] == "ran"}
    assert {r["gate_id"] for r in result["refs"]} <= ran


def test_live_verify_is_accepted(ledger: Ledger, key: str):
    decision_id = ledger.submit(example("gate.json")).json()["decision_id"]

    response = _verify(ledger, decision_id, key, live=True)

    assert response.status_code == 200, response.text
    _check_contract(response.json(), decision_id=decision_id, live=True)


def test_a_record_cites_only_what_its_gates_that_ran_cite(ledger: Ledger, key: str):
    receipt = ledger.submit(example("record.json")).json()

    result = _verify(ledger, receipt["decision_id"], key).json()

    _check_contract(result, decision_id=receipt["decision_id"], live=False)
    ran = {g["gate_id"] for g in receipt["gates"] if g["status"] == "ran"}
    assert {r["gate_id"] for r in result["refs"]} <= ran
    if not ran:  # a deployment that mandates no gate: nothing ran on a record
        assert result["refs"] == []


def test_verify_changes_nothing(ledger: Ledger, key: str):
    location = ledger.submit(example("gate.json")).headers["location"]
    decision_id = location.rsplit("/", 1)[1]
    before = ledger.get(location, key=key).json()

    _verify(ledger, decision_id, key)
    _verify(ledger, decision_id, key, live=True)

    assert ledger.get(location, key=key).json() == before


def test_another_reasoners_decision_and_an_unknown_one_are_not_found(
    ledger: Ledger, key: str, other_key: str
):
    decision_id = ledger.submit(example("gate.json")).json()["decision_id"]

    problem(_verify(ledger, decision_id, other_key), 404, "DECISION_NOT_FOUND")
    problem(
        _verify(ledger, "0f3a9d1e-7a2b-4c3d-9e8f-1a2b3c4d5e6f", key),
        404,
        "DECISION_NOT_FOUND",
    )
