"""Every error the ledger serves is an RFC 9457 problem document.

The ledger's own refusals (``LedgerError``) are tested with the routes that
raise them. These cases pin the shape of the errors FastAPI and Starlette
raise themselves — an unknown route, a wrong method, an invalid query
parameter — and of an unhandled exception, so a client parses one format.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from app.main import create_app
from core.exceptions import LedgerError

if TYPE_CHECKING:
    import pytest

    from tests.app.conftest import Ledger

_PROBLEM = "application/problem+json"


def test_an_unknown_route_is_a_not_found_problem(ledger: Ledger):
    response = ledger.get("/api/v1/nothing-here")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(_PROBLEM)
    body = response.json()
    assert body["code"] == "NOT_FOUND"
    assert body["instance"] == "/api/v1/nothing-here"


def test_a_wrong_method_is_a_method_not_allowed_problem(ledger: Ledger):
    response = ledger.client.delete("/api/v1/decisions")
    assert response.status_code == 405
    assert response.json()["code"] == "METHOD_NOT_ALLOWED"
    assert "allow" in response.headers


def test_an_invalid_query_parameter_is_a_request_problem(ledger: Ledger, submission):
    response = ledger.get(
        f"/api/v1/entities/{submission.entity_id}/decisions",
        params={"limit": "many"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "REQUEST_INVALID"
    assert body["errors"][0]["loc"] == ["query", "limit"]


def test_an_unhandled_error_is_an_internal_problem(
    ledger: Ledger, submission, monkeypatch: pytest.MonkeyPatch
):
    def _broken(*args, **kwargs):
        raise RuntimeError("the database caught fire")

    monkeypatch.setattr(ledger.store, "lookup", _broken)
    client = TestClient(ledger.client.app, raise_server_exceptions=False)
    response = client.get(
        "/api/v1/decisions/0f3a9d1e-7a2b-4c3d-9e8f-1a2b3c4d5e6f",
        headers={"X-DecisionLedger-Key": "stub-reasoner-test-key"},
    )
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "INTERNAL_ERROR"
    assert "fire" not in response.text


def test_a_problem_document_has_the_standard_members():
    err = LedgerError(409, "DECISION_ALREADY_RESOLVED", "it is", extra={"n": 1})
    assert err.problem(instance="/x") == {
        "type": "urn:decisionledger:problem:decision-already-resolved",
        "title": "Decision already resolved",
        "status": 409,
        "detail": "it is",
        "code": "DECISION_ALREADY_RESOLVED",
        "instance": "/x",
        "n": 1,
    }


def _refs(node: object) -> list[str]:
    if isinstance(node, dict):
        found = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
        return found + [r for value in node.values() for r in _refs(value)]
    if isinstance(node, list):
        return [r for value in node for r in _refs(value)]
    return []


def test_the_openapi_document_has_no_dangling_reference():
    spec = create_app(plugins=[], gate_plugins=[]).openapi()
    schemas = spec["components"]["schemas"]
    refs = _refs(spec)
    assert refs
    dangling = {
        ref
        for ref in refs
        if not ref.startswith("#/components/schemas/")
        or ref.removeprefix("#/components/schemas/") not in schemas
    }
    assert not dangling
    body = spec["paths"]["/api/v1/decisions"]["post"]["requestBody"]
    assert body["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Submission"
    }
    assert schemas["Submission"]["title"] == "Submission"


def test_every_api_operation_documents_its_problems():
    spec = create_app(plugins=[], gate_plugins=[]).openapi()
    problem_schema = {"$ref": "#/components/schemas/ProblemDocument"}
    for path, operations in spec["paths"].items():
        if not path.startswith("/api/v1"):
            continue
        for method, operation in operations.items():
            unauthorized = operation["responses"].get("401")
            assert unauthorized is not None, (method, path)
            content = unauthorized["content"]
            assert content == {"application/problem+json": {"schema": problem_schema}}
