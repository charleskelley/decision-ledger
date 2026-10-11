"""Behavioral tests for the framework-owned decision routes in ``app.main``.

``GET /api/v1/decisions/{id}`` and ``POST /api/v1/decisions/{id}/replay`` are
reasoner-agnostic: they key on ``decision_id`` and dispatch through the
bundle store. The store can fail in two distinct ways that an operator must
be able to tell apart:

- ``KeyError`` — no bundle exists → ``404 DECISION_NOT_FOUND``.
- ``UnregisteredReasonerError`` — the bundle exists but was written by a
  reasoner that is not composed in this deployment (installed elsewhere, or
  excluded by ``DECISIONLEDGER_REASONERS``) → ``409 REASONER_NOT_COMPOSED``
  naming the ``reasoner_id``. Before this envelope existed the error escaped
  as an unstructured 500.

The lifespan's ``build_framework_services`` is replaced by a fake whose store
raises on demand; no infrastructure is touched and no plugin is composed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.audit.store import BundleStore
from app.main import create_app
from core.exceptions import UnregisteredReasonerError

if TYPE_CHECKING:
    from collections.abc import Iterator

_DECISION_ID = "0f3a9d1e-7a2b-4c3d-9e8f-1a2b3c4d5e6f"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Fake bundle store exposed on ``app.state.framework.store``.

    ``FrameworkServices`` is a dataclass, so its fields are not class
    attributes and a ``spec`` mock of it would not expose ``store``; the
    store is attached explicitly with the real ``BundleStore`` as its spec.
    """
    bundle_store = MagicMock(spec=BundleStore)
    framework = MagicMock(name="framework", store=bundle_store)
    monkeypatch.setattr(
        "app.main.build_framework_services", lambda settings, plugins: framework
    )
    return bundle_store


@pytest.fixture
def client(store: MagicMock) -> Iterator[TestClient]:
    with TestClient(create_app(plugins=[])) as test_client:
        yield test_client


def test_get_decision_returns_404_envelope_when_bundle_is_missing(
    client: TestClient, store: MagicMock
):
    store.load.side_effect = KeyError(_DECISION_ID)

    response = client.get(f"/api/v1/decisions/{_DECISION_ID}")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["error_code"] == "DECISION_NOT_FOUND"
    assert _DECISION_ID in detail["message"]


def test_get_decision_returns_409_envelope_when_reasoner_is_not_composed(
    client: TestClient, store: MagicMock
):
    store.load.side_effect = UnregisteredReasonerError("other-reasoner")

    response = client.get(f"/api/v1/decisions/{_DECISION_ID}")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error_code"] == "REASONER_NOT_COMPOSED"
    assert "other-reasoner" in detail["message"]
    assert detail["details"] == {"reasoner_id": "other-reasoner"}


def test_replay_returns_404_envelope_when_bundle_is_missing(
    client: TestClient, store: MagicMock
):
    store.replay.side_effect = KeyError(_DECISION_ID)

    response = client.post(f"/api/v1/decisions/{_DECISION_ID}/replay")

    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "DECISION_NOT_FOUND"


def test_replay_returns_409_envelope_when_reasoner_is_not_composed(
    client: TestClient, store: MagicMock
):
    store.replay.side_effect = UnregisteredReasonerError("other-reasoner")

    response = client.post(f"/api/v1/decisions/{_DECISION_ID}/replay")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error_code"] == "REASONER_NOT_COMPOSED"
    assert detail["details"] == {"reasoner_id": "other-reasoner"}


def test_replay_reports_the_store_result_when_dispatch_succeeds(
    client: TestClient, store: MagicMock
):
    store.replay.return_value = SimpleNamespace(
        decision_id=_DECISION_ID,
        original_action="HOLD",
        replayed_action="HOLD",
        actions_match=True,
    )

    response = client.post(f"/api/v1/decisions/{_DECISION_ID}/replay")

    assert response.status_code == 200
    assert response.json() == {
        "decision_id": _DECISION_ID,
        "original_action": "HOLD",
        "replayed_action": "HOLD",
        "actions_match": True,
    }
    store.replay.assert_called_once_with(_DECISION_ID)
