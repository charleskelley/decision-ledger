"""Behavioral tests for the ATO router's request handling.

``LoginEvent`` is a strict model. These tests pin that the endpoint accepts
the ordinary JSON wire format (ISO-8601 timestamps, enum values as strings)
by validating the body in JSON mode, and that an invalid body is a 422 with
Pydantic's error list rather than a 500.

The pipeline itself is not exercised: ``run_ato_decision`` is replaced with
a stub that returns a minimal bundle-like object.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ato_reasoner.api as api_module
from ato_reasoner.registry import ATO_REGISTRATION
from core.actions import DecisionAction
from core.routes import GateRoute


def _wire_body() -> dict[str, Any]:
    return {
        "event_id": "evt-api-001",
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "account_id": "acct-api",
        "session_id": "sess-api",
        "ip_address": "203.0.113.10",
        "geolocation": {
            "latitude": 37.77,
            "longitude": -122.42,
            "country": "US",
            "city": "San Francisco",
            "asn": "AS7922",
        },
        "device_fingerprint": "fp-api",
        "user_agent": "Mozilla/5.0",
        "auth_method": "MFA_TOTP",
        "outcome": "SUCCESS",
    }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    seen: dict[str, Any] = {}

    async def _fake_run(*, event: Any, services: Any, framework: Any, **kw: Any):
        del services, framework, kw
        seen["event"] = event
        return SimpleNamespace(
            decision_id="00000000-0000-0000-0000-000000000001",
            decision_action=DecisionAction.ALLOW,
            enforcement_rule_applied=None,
            raw_event=SimpleNamespace(route=GateRoute.FAST_PATH_ALLOW),
            latency_breakdown={"features_ms": 1.0, "scorer_ms": 2.0},
        )

    monkeypatch.setattr(api_module, "run_ato_decision", _fake_run)
    app = FastAPI()
    app.include_router(api_module.router)
    app.state.reasoners = {ATO_REGISTRATION.reasoner_id: object()}
    app.state.framework = object()
    client = TestClient(app)
    client.seen = seen  # type: ignore[attr-defined]
    return client


def test_decision_endpoint_accepts_iso_timestamps_and_enum_strings(client):
    response = client.post("/api/v1/ato/decisions", json=_wire_body())

    assert response.status_code == 200, response.text
    event = client.seen["event"]  # type: ignore[attr-defined]
    assert event.auth_method.value == "MFA_TOTP"
    assert event.outcome.value == "SUCCESS"
    assert event.timestamp.tzinfo is not None
    body = response.json()
    assert body["decision_action"] == "ALLOW"
    assert body["route"] == "FAST_PATH_ALLOW"
    assert body["latency_ms"] == 3.0


def test_decision_endpoint_rejects_invalid_body_with_422_and_field_errors(client):
    bad = _wire_body()
    bad["auth_method"] = "CARRIER_PIGEON"
    del bad["account_id"]

    response = client.post(
        "/api/v1/ato/decisions",
        content=json.dumps(bad),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    locs = {tuple(err["loc"]) for err in response.json()["detail"]}
    assert ("auth_method",) in locs
    assert ("account_id",) in locs


def test_decision_endpoint_documents_the_login_event_schema_in_openapi(client):
    schema = client.app.openapi()  # type: ignore[attr-defined]
    body = schema["paths"]["/api/v1/ato/decisions"]["post"]["requestBody"]

    assert body["required"] is True
    props = body["content"]["application/json"]["schema"]["properties"]
    assert {"event_id", "timestamp", "auth_method", "outcome"} <= set(props)
