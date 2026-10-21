"""Registrations written over HTTP (DR-33) — the operator key's suite.

``PUT /api/v1/reasoners/{reasoner_id}`` creates a reasoner's registration
(201, ``source: api``) or replaces its API registration with a new version
(200; earlier versions stay readable); an unchanged registration is 200
with the same version. Only the operator key registers. A plugin's or the
registration file's reasoner cannot be replaced (409); a body that is not a
registration for the path's reasoner, a native namespace another
registration declares differently, and a gate set the instance cannot serve
are 422, and a refused write stores nothing.

The probe reasoner (``examples/registration.json``, ``conformance-reasoner``)
binds no gate and keeps one id across runs: registrations cannot be deleted
over the API, so the suite registers one reasoner per instance, not one per
run. Refused writes use fresh ids (nothing is stored). The instance must not
mandate a gate that refuses the empty configuration
(``DECISIONLEDGER_MANDATORY_GATES``): the probe binds none.
"""

from __future__ import annotations

import uuid

import pytest

from conformance._ledger import VERSION, Ledger, problem, registration

PROBE = "/api/v1/reasoners/conformance-reasoner"


def _fresh_id() -> str:
    return f"conformance-refused-{uuid.uuid4()}"


def test_the_operator_registers_a_reasoner_over_http(ledger: Ledger, operator_key: str):
    response = ledger.put(PROBE, registration(), key=operator_key)

    # 201 the first time this instance sees the probe; 200 on a later run.
    assert response.status_code in (200, 201), response.text
    body = response.json()
    if response.status_code == 201:
        assert response.headers["location"] == PROBE
    assert body["reasoner_id"] == "conformance-reasoner"
    assert body["source"] == "api"
    assert body["registered_by"]
    assert body["registered_at"]
    assert body["updated_at"]
    assert VERSION.match(body["registration_version"])
    assert body["registration"]["reasoner_id"] == "conformance-reasoner"
    assert ledger.get(PROBE, key=operator_key).json() == body
    listed = ledger.get("/api/v1/reasoners", key=operator_key).json()["reasoners"]
    assert body in listed


def test_an_unchanged_registration_is_not_a_new_version(
    ledger: Ledger, operator_key: str
):
    first = ledger.put(PROBE, registration(), key=operator_key).json()

    again = ledger.put(PROBE, registration(), key=operator_key)

    assert again.status_code == 200
    assert again.json()["registration_version"] == first["registration_version"]
    assert again.json()["updated_at"] == first["updated_at"]


def test_a_changed_registration_is_a_new_version_and_the_old_one_stays_readable(
    ledger: Ledger, operator_key: str
):
    first = ledger.put(
        PROBE,
        registration(reasoner_name=f"Conformance {uuid.uuid4()}"),
        key=operator_key,
    ).json()

    response = ledger.put(
        PROBE,
        registration(reasoner_name=f"Conformance {uuid.uuid4()}"),
        key=operator_key,
    )

    assert response.status_code == 200
    second = response.json()
    assert second["registration_version"] != first["registration_version"]
    assert second["registered_at"] == first["registered_at"]
    old = ledger.get(
        f"{PROBE}/registrations/{first['registration_version']}", key=operator_key
    )
    assert old.status_code == 200
    assert old.json()["registration"]["reasoner_name"] == first["reasoner_name"]


def test_only_the_operator_registers(ledger: Ledger, key: str):
    reasoner_id = _fresh_id()
    body = registration(reasoner_id=reasoner_id)
    path = f"/api/v1/reasoners/{reasoner_id}"

    problem(ledger.put(path, body, key=key), 403, "OPERATOR_REQUIRED")
    problem(ledger.put(path, body, key=None), 401, "UNAUTHENTICATED")
    problem(ledger.get(path, key=key), 404, "REASONER_NOT_FOUND")


def test_a_declared_registration_cannot_be_replaced_over_http(
    ledger: Ledger, operator_key: str
):
    reasoners = ledger.get("/api/v1/reasoners", key=operator_key).json()["reasoners"]
    declared = [r for r in reasoners if r["source"] != "api"]
    if not declared:
        pytest.skip("the instance registers every reasoner over the API")
    entry = declared[0]
    changed = {**entry["registration"], "reasoner_name": "Replaced over HTTP"}

    body = problem(
        ledger.put(
            f"/api/v1/reasoners/{entry['reasoner_id']}", changed, key=operator_key
        ),
        409,
        "REGISTRATION_SOURCE_IMMUTABLE",
    )

    assert body["source"] == entry["source"]
    served = ledger.get(f"/api/v1/reasoners/{entry['reasoner_id']}", key=operator_key)
    assert served.json()["registration_version"] == entry["registration_version"]


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        {"reasoner_name": "No id, no gate set"},
        {"reasoner_id": "someone-else", "reasoner_name": "Wrong id", "gates": []},
    ],
    ids=["not-json", "incomplete", "other-reasoner-id"],
)
def test_a_body_that_is_not_a_registration_for_the_path_is_refused(
    ledger: Ledger, operator_key: str, body: object
):
    reasoner_id = _fresh_id()
    path = f"/api/v1/reasoners/{reasoner_id}"

    problem(ledger.put(path, body, key=operator_key), 422, "REGISTRATION_INVALID")
    problem(ledger.get(path, key=operator_key), 404, "REASONER_NOT_FOUND")


def test_a_mandatory_binding_to_a_gate_the_instance_does_not_compose_is_refused(
    ledger: Ledger, operator_key: str
):
    reasoner_id = _fresh_id()
    path = f"/api/v1/reasoners/{reasoner_id}"
    gate_id = f"absent-gate-{uuid.uuid4().hex[:8]}"
    body = registration(
        reasoner_id=reasoner_id,
        gates=[{"gate_id": gate_id, "mandatory": True}],
    )

    refused = problem(
        ledger.put(path, body, key=operator_key), 422, "GATE_BINDING_REFUSED"
    )

    assert refused["gate_id"] == gate_id
    problem(ledger.get(path, key=operator_key), 404, "REASONER_NOT_FOUND")


def test_a_native_namespace_declared_differently_is_refused(
    ledger: Ledger, operator_key: str
):
    reasoners = ledger.get("/api/v1/reasoners", key=operator_key).json()["reasoners"]
    declared = [
        namespace
        for r in reasoners
        for namespace in r["registration"].get("native_namespaces", [])
    ]
    if not declared:
        pytest.skip("no registration on this instance declares a native namespace")
    namespace = declared[0]
    reasoner_id = _fresh_id()
    path = f"/api/v1/reasoners/{reasoner_id}"
    body = registration(
        reasoner_id=reasoner_id,
        native_namespaces=[{**namespace, "namespace_uuid": str(uuid.uuid4())}],
    )

    refused = problem(
        ledger.put(path, body, key=operator_key), 422, "NATIVE_NAMESPACE_CONFLICT"
    )

    assert namespace["native_namespace"] in refused["detail"]
    problem(ledger.get(path, key=operator_key), 404, "REASONER_NOT_FOUND")
