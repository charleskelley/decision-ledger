"""ATO runs as its own service and submits to the ledger through a client.

``ato_reasoner.app:create_app`` builds ATO's services and a ledger client
(DR-31):

- with no ``ATO_LEDGER_URL`` the ledger runs in ATO's process
  (``LocalClient.from_settings``); with one, ATO calls it over HTTP with
  ``ATO_LEDGER_KEY``;
- the app serves ``/health`` and ``POST /api/v1/ato/decisions`` through its
  client, and closes the client and its services on shutdown;
- a ledger refusal reaches ATO's caller as the ledger's problem document.

No infrastructure: the services and the client are doubles.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.models import DecisionReceipt
from app.client import HttpClient, LocalClient
from ato_reasoner import app as app_module
from ato_reasoner.app import create_app, ledger_client
from ato_reasoner.scorer import ScorerOutput
from ato_reasoner.settings import AtoSettings
from core.actions import DecisionAction
from core.exceptions import LedgerError


def test_without_a_ledger_url_the_ledger_runs_in_process(
    monkeypatch: pytest.MonkeyPatch,
):
    local = MagicMock(spec=LocalClient)
    monkeypatch.setattr(
        app_module.LocalClient, "from_settings", classmethod(lambda cls, s: local)
    )
    assert ledger_client(AtoSettings(ledger_url="")) is local


async def test_with_a_ledger_url_ato_calls_it_over_http_with_its_key():
    client = ledger_client(
        AtoSettings(ledger_url=" http://ledger:8000 ", ledger_key=SecretStr("k"))
    )
    assert isinstance(client, HttpClient)
    assert client._api_key == "k"
    assert str(client._http.base_url) == "http://ledger:8000"
    await client.aclose()


class _Services:
    def __init__(self, features: Any, scorer: Any) -> None:
        self.feature_svc = MagicMock()
        self.feature_svc.compute.return_value = features
        self.scorer = MagicMock()
        self.scorer.score.return_value = scorer
        self.template_id = "ato-v4"
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Client:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.submitted: list[Any] = []
        self.closed = False

    async def submit(self, submission: Any) -> DecisionReceipt:
        if self.error is not None:
            raise self.error
        self.submitted.append(submission)
        return DecisionReceipt(
            decision_id="d-1",
            reasoner_id=submission.reasoner_id,
            event_id=submission.event_id,
            entity_id=str(submission.entity_id),
            mode=submission.mode,
            decision_action=DecisionAction.ALLOW,
            decision_level_rule=None,
            enforcement_rule_applied=None,
            bypass_rule_id="allow-band",
            bypass_rule_holds=True,
            registration_version="v",
            gates=[],
            latency_breakdown={},
            created_at=datetime.now(UTC),
        )

    async def aclose(self) -> None:
        self.closed = True


def _app(services: _Services, client: _Client):
    return create_app(
        AtoSettings(),
        services_factory=lambda settings: services,  # type: ignore[arg-type,return-value]
        client_factory=lambda settings: client,  # type: ignore[arg-type,return-value]
    )


@pytest.fixture
def services(ato_feature_vector) -> _Services:
    scorer = ScorerOutput(
        entity_id=ato_feature_vector.entity_id,
        risk_score=0.05,
        top_signals=[],
        scorer_version="xgb-test",
        inference_latency_ms=1.0,
    )
    return _Services(ato_feature_vector, scorer)


def test_the_service_decides_through_its_client_and_closes_it(
    services: _Services, login_event
):
    client = _Client()
    app = _app(services, client)

    with TestClient(app) as http:
        assert http.get("/health").json() == {"status": "ok"}
        response = http.post(
            "/api/v1/ato/decisions",
            content=login_event.model_dump_json(),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["decision_id"], body["decision_action"]) == ("d-1", "ALLOW")
    [submission] = client.submitted
    assert submission.event_id == login_event.event_id
    assert client.closed
    assert services.closed


def test_a_ledger_refusal_is_passed_through_as_its_problem(
    services: _Services, login_event
):
    refusal = LedgerError(403, "REASONER_MISMATCH", "wrong key", extra={"x": 1})
    with TestClient(_app(services, _Client(refusal))) as http:
        response = http.post(
            "/api/v1/ato/decisions",
            content=login_event.model_dump_json(),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "REASONER_MISMATCH"
    assert response.json()["x"] == 1


def test_a_client_that_cannot_be_built_closes_the_services(services: _Services):
    def _broken(settings: Any) -> Any:
        raise RuntimeError("no ledger")

    app = create_app(
        AtoSettings(),
        services_factory=lambda settings: services,  # type: ignore[arg-type,return-value]
        client_factory=_broken,
    )
    with pytest.raises(RuntimeError, match="no ledger"), TestClient(app):
        pass
    assert services.closed


def test_an_unreachable_ledger_is_a_503_that_names_no_secret(
    services: _Services, login_event
):
    import httpx

    unreachable = httpx.LocalProtocolError("Illegal header value b'secret-key '")
    with TestClient(_app(services, _Client(unreachable))) as http:  # type: ignore[arg-type]
        response = http.post(
            "/api/v1/ato/decisions",
            content=login_event.model_dump_json(),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "LEDGER_UNAVAILABLE"
    assert "secret-key" not in response.text
