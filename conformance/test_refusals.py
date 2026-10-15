"""Every refusal is a problem document with a stable code, and writes nothing.

Invalid bodies, a submission its registration does not allow, missing or
unknown keys, another reasoner's key, and the operator key on a write.
"""

from __future__ import annotations

from conformance._ledger import Ledger, example, problem


def _not_written(ledger: Ledger, key: str, entity_id: str) -> None:
    timeline = ledger.get(f"/api/v1/entities/{entity_id}/decisions", key=key).json()
    assert timeline["decisions"] == []


def test_a_body_that_is_not_a_submission_is_422(ledger: Ledger, key: str):
    body = problem(
        ledger.post("/api/v1/decisions", {"mode": "record"}, key=key),
        422,
        "SUBMISSION_INVALID",
    )
    assert any(error["loc"] == ["reasoner_id"] for error in body["errors"])
    problem(
        ledger.post("/api/v1/decisions", b"not json", key=key),
        422,
        "SUBMISSION_INVALID",
    )


def test_a_record_without_its_citation_is_422(ledger: Ledger, key: str):
    submission = example("record.json")
    submission.pop("bypass")
    problem(ledger.submit(submission), 422, "SUBMISSION_INVALID")
    _not_written(ledger, key, submission["entity_id"])


def test_an_unregistered_bypass_rule_is_422(ledger: Ledger, key: str):
    submission = example("unregistered-rule.json")
    body = problem(ledger.submit(submission), 422, "BYPASS_RULE_UNREGISTERED")
    assert body["reasoner_id"] == "example-reasoner"
    _not_written(ledger, key, submission["entity_id"])


def test_no_key_or_an_unknown_key_is_401(ledger: Ledger, key: str):
    submission = example("record.json")
    problem(
        ledger.post("/api/v1/decisions", submission, key=None), 401, "UNAUTHENTICATED"
    )
    problem(
        ledger.post("/api/v1/decisions", submission, key="not-a-key"),
        401,
        "UNAUTHENTICATED",
    )
    _not_written(ledger, key, submission["entity_id"])


def test_another_reasoners_key_cannot_write_or_read(
    ledger: Ledger, key: str, other_key: str
):
    submission = example("record.json")
    body = problem(ledger.submit(submission, key=other_key), 403, "REASONER_MISMATCH")
    assert body["reasoner_id"] == "example-reasoner"
    _not_written(ledger, key, submission["entity_id"])

    decision_id = ledger.submit(example("record.json")).json()["decision_id"]
    problem(
        ledger.get(f"/api/v1/decisions/{decision_id}", key=other_key),
        404,
        "DECISION_NOT_FOUND",
    )


def test_the_operator_key_writes_no_decision(
    ledger: Ledger, key: str, operator_key: str
):
    submission = example("record.json")
    problem(ledger.submit(submission, key=operator_key), 403, "REASONER_KEY_REQUIRED")
    _not_written(ledger, key, submission["entity_id"])


def test_errors_outside_the_api_are_problems_too(ledger: Ledger, key: str):
    problem(ledger.get("/api/v1/no-such-route", key=key), 404, "NOT_FOUND")
