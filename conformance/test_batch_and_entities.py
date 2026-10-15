"""``POST /api/v1/decisions:batch`` and ``GET /api/v1/entities/{id}/decisions``.

A batch records each recorded line and rejects the rest on their own (a
gated line: ``BATCH_RECORD_ONLY``); the subject's timeline then lists the
recorded decisions, newest first.
"""

from __future__ import annotations

import json
import uuid

from conformance._ledger import EXAMPLES, Ledger, fresh, problem


def _batch_lines(entity_id: str) -> list[dict]:
    lines = (EXAMPLES / "batch.ndjson").read_text(encoding="utf-8").splitlines()
    return [fresh(json.loads(line), entity_id=entity_id) for line in lines]


def test_a_batch_records_its_records_and_rejects_its_gate_line(
    ledger: Ledger, key: str
):
    entity_id = str(uuid.uuid4())
    lines = _batch_lines(entity_id)
    body = "".join(json.dumps(line) + "\n" for line in lines)

    response = ledger.post("/api/v1/decisions:batch", body, key=key)

    assert response.status_code == 200, response.text
    result = response.json()
    assert (result["recorded"], result["rejected"]) == (3, 1)
    assert [r["line"] for r in result["results"]] == [1, 2, 3, 4]
    assert [r["status"] for r in result["results"]] == ["recorded"] * 3 + ["rejected"]
    assert result["results"][3]["code"] == "BATCH_RECORD_ONLY"

    timeline = ledger.get(f"/api/v1/entities/{entity_id}/decisions", key=key)
    assert timeline.status_code == 200
    decisions = timeline.json()["decisions"]
    assert {d["decision_id"] for d in decisions} == {
        r["decision_id"] for r in result["results"][:3]
    }
    created = [d["created_at"] for d in decisions]
    assert created == sorted(created, reverse=True)
    assert {d["reasoner_id"] for d in decisions} == {"example-reasoner"}


def test_an_empty_batch_is_422(ledger: Ledger, key: str):
    problem(ledger.post("/api/v1/decisions:batch", "\n", key=key), 422, "BATCH_EMPTY")


def test_a_timeline_needs_a_uuid(ledger: Ledger, key: str):
    problem(
        ledger.get("/api/v1/entities/not-a-uuid/decisions", key=key),
        422,
        "ENTITY_ID_INVALID",
    )
