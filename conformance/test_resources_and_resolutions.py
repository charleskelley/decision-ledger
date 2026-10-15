"""Registrations, gates, and human resolutions.

The registrations resource serves the example's registration under the
version every receipt names, and that version stays readable; the gates
resource describes each composed gate; the operator resolves a held
decision once, and only a held or challenged one.
"""

from __future__ import annotations

import json

from conformance._ledger import EXAMPLES, Ledger, example, problem


def test_the_registrations_resource_serves_the_cited_version(ledger: Ledger, key: str):
    receipt = ledger.submit(example("record.json")).json()
    version = receipt["registration_version"]

    reasoners = ledger.get("/api/v1/reasoners", key=key).json()["reasoners"]
    [entry] = [r for r in reasoners if r["reasoner_id"] == "example-reasoner"]
    assert entry["registration_version"] == version
    assert entry["registration"]["reasoner_id"] == "example-reasoner"

    stored = ledger.get(
        f"/api/v1/reasoners/example-reasoner/registrations/{version}", key=key
    )
    assert stored.status_code == 200
    assert stored.json()["registration"] == entry["registration"]
    problem(
        ledger.get("/api/v1/reasoners/no-such-reasoner", key=key),
        404,
        "REASONER_NOT_FOUND",
    )


def test_the_gates_resource_describes_each_gate(ledger: Ledger, key: str):
    response = ledger.get("/api/v1/gates", key=key)
    assert response.status_code == 200
    for gate in response.json()["gates"]:
        assert set(gate) >= {
            "gate_id",
            "kind",
            "deterministic",
            "mandatory",
            "config_schema",
        }
        assert gate["kind"] in {"llm", "lookup", "rules"}


def _resolution() -> str:
    return (EXAMPLES / "resolution.json").read_text(encoding="utf-8")


def test_the_operator_resolves_a_held_decision_once(
    ledger: Ledger, key: str, operator_key: str
):
    receipt = ledger.submit(example("held-record.json")).json()
    assert receipt["decision_action"] == "HOLD"
    path = f"/api/v1/decisions/{receipt['decision_id']}/resolutions"

    problem(ledger.post(path, _resolution(), key=key), 403, "OPERATOR_REQUIRED")
    response = ledger.post(path, _resolution(), key=operator_key)

    assert response.status_code == 201, response.text
    resolved = response.json()
    assert resolved["resolution_action"] == "ALLOW"
    assert resolved["realized_action"] == "ALLOW"
    assert resolved["status"] == "COMPLETED"
    problem(
        ledger.post(path, _resolution(), key=operator_key),
        409,
        "DECISION_ALREADY_RESOLVED",
    )
    # The decision itself is immutable.
    bundle = ledger.get(f"/api/v1/decisions/{receipt['decision_id']}", key=key)
    assert bundle.json()["decision_action"] == "HOLD"


def test_only_a_held_or_challenged_decision_is_resolved(
    ledger: Ledger, key: str, operator_key: str
):
    decision_id = ledger.submit(example("record.json")).json()["decision_id"]
    path = f"/api/v1/decisions/{decision_id}/resolutions"
    problem(
        ledger.post(path, _resolution(), key=operator_key),
        409,
        "DECISION_NOT_RESOLVABLE",
    )
    not_terminal = json.dumps(
        {**json.loads(_resolution()), "resolution_action": "HOLD"}
    )
    problem(
        ledger.post(path, not_terminal, key=operator_key), 422, "RESOLUTION_INVALID"
    )
