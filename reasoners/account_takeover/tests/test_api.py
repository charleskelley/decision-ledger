"""Behavioral tests for the ATO router's request handling.

``LoginEvent`` is a strict model. These tests pin that the endpoint accepts
the ordinary JSON wire format (ISO-8601 timestamps, enum values as strings)
by validating the body in JSON mode, and that an invalid body is a 422 with
Pydantic's error list rather than a 500 — including every login its
framework submission would refuse, rejected before the pipeline runs.

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
            decision_level_rule=None,
            submission=SimpleNamespace(
                event_id=event.event_id,
                mode="record",
                bypass=SimpleNamespace(rule_id="allow-band"),
            ),
            bypass_rule_holds=True,
            bypass_check=None,
            other_bypass_checks=(),
            gates=(
                SimpleNamespace(
                    gate_id="policy",
                    mandatory=False,
                    status="skipped",
                    enforcement=None,
                    enforced_action=None,
                ),
            ),
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
    assert body["mode"] == "record"
    assert body["bypass_rule_id"] == "allow-band"
    assert body["bypass_rule_holds"] is True
    assert body["enforcement_rule_applied"] is None
    assert body["gates"] == [
        {"gate_id": "policy", "mandatory": False, "status": "skipped", "action": None}
    ]
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


@pytest.mark.parametrize(
    ("field", "value", "loc"),
    [
        ("timestamp", "2026-10-14T12:00:00", ("timestamp",)),
        ("event_id", "", ("event_id",)),
        (
            "geolocation",
            {**_wire_body()["geolocation"], "latitude": float("nan")},
            ("geolocation", "latitude"),
        ),
        ("user_agent", "Mozilla/5.0\x00", ()),
    ],
    ids=["naive-timestamp", "empty-event-id", "nan-latitude", "nul"],
)
def test_a_login_its_submission_would_refuse_is_a_422_before_the_pipeline_runs(
    client, field, value, loc
):
    # json.dumps writes the NaN literal; the pipeline (features, Redis
    # windows) must never see the event.
    bad = {**_wire_body(), field: value}
    response = client.post(
        "/api/v1/ato/decisions",
        content=json.dumps(bad),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422, response.text
    assert "event" not in client.seen  # type: ignore[attr-defined]
    if loc:
        assert loc in {tuple(err["loc"]) for err in response.json()["detail"]}


def test_decision_endpoint_documents_the_login_event_schema_in_openapi(client):
    schema = client.app.openapi()  # type: ignore[attr-defined]
    body = schema["paths"]["/api/v1/ato/decisions"]["post"]["requestBody"]

    assert body["required"] is True
    props = body["content"]["application/json"]["schema"]["properties"]
    assert {"event_id", "timestamp", "auth_method", "outcome"} <= set(props)
