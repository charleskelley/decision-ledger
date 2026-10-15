"""``POST /api/v1/decisions``, lookup, and replay (examples: record, gate).

A record is written and answered 201 with its receipt and ``Location``; the
bundle at that location is schema version 2 and holds the submission as
sent; replay reproduces it under the registration version the receipt
names. A gated submission runs the example's policy gate.
"""

from __future__ import annotations

from conformance._ledger import VERSION, Ledger, example, problem


def test_a_record_is_written_with_its_receipt(ledger: Ledger, key: str):
    submission = example("record.json")
    response = ledger.submit(submission)

    assert response.status_code == 201, response.text
    receipt = response.json()
    assert response.headers["location"] == f"/api/v1/decisions/{receipt['decision_id']}"
    assert receipt["mode"] == "record"
    assert receipt["decision_action"] == "ALLOW"
    assert receipt["bypass_rule_id"] == "low-score"
    assert receipt["bypass_rule_holds"] is True
    assert receipt["event_id"] == submission["event_id"]
    assert receipt["entity_id"] == submission["entity_id"]
    assert VERSION.match(receipt["registration_version"])
    # A recorded decision skips its advisory gates (a deployment that
    # mandates the policy gate runs it instead).
    [policy] = [g for g in receipt["gates"] if g["gate_id"] == "policy"]
    assert policy["status"] in {"skipped", "ran"}


def test_the_written_bundle_holds_the_submission_as_sent(ledger: Ledger, key: str):
    submission = example("record.json")
    location = ledger.submit(submission).headers["location"]

    response = ledger.get(location, key=key)

    assert response.status_code == 200
    bundle = response.json()
    assert bundle["schema_version"] == 2
    assert bundle["submission"]["event_id"] == submission["event_id"]
    assert bundle["submission"]["raw_event"] == submission["raw_event"]
    assert bundle["submission"]["reasoner_context"] == submission["reasoner_context"]
    assert bundle["bypass_check"]["rule"]["rule_id"] == "low-score"
    # The example binds the policy gate; a deployment may mandate more.
    assert "policy" in [g["gate_id"] for g in bundle["gates"]]


def test_a_record_replays_to_a_match(ledger: Ledger, key: str):
    receipt = ledger.submit(example("record.json")).json()

    response = ledger.post(
        f"/api/v1/decisions/{receipt['decision_id']}/replay", b"", key=key
    )

    assert response.status_code == 200, response.text
    replay = response.json()
    assert replay["matched"] is True
    assert replay["replayed_action"] == receipt["decision_action"]
    assert replay["registration_version"] == receipt["registration_version"]


def test_a_gated_submission_runs_the_policy_gate(ledger: Ledger, key: str):
    response = ledger.submit(example("gate.json"))

    assert response.status_code == 201, response.text
    receipt = response.json()
    assert receipt["mode"] == "gate"
    assert receipt["bypass_rule_id"] is None
    assert receipt["decision_action"] in {"ALLOW", "CHALLENGE", "HOLD", "BLOCK"}
    [policy] = [g for g in receipt["gates"] if g["gate_id"] == "policy"]
    assert policy["status"] in {"ran", "failed"}
    assert policy["action"] in {"ALLOW", "CHALLENGE", "HOLD", "BLOCK"}


def test_an_unknown_decision_is_not_found(ledger: Ledger, key: str):
    for decision_id in ("0f3a9d1e-7a2b-4c3d-9e8f-1a2b3c4d5e6f", "not-a-uuid"):
        problem(
            ledger.get(f"/api/v1/decisions/{decision_id}", key=key),
            404,
            "DECISION_NOT_FOUND",
        )
        problem(
            ledger.post(f"/api/v1/decisions/{decision_id}/replay", b"", key=key),
            404,
            "DECISION_NOT_FOUND",
        )
