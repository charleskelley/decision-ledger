"""Behavioral tests for the framework-owned decision routes in ``app.main``.

``GET /api/v1/decisions/{id}`` and ``POST /api/v1/decisions/{id}/replay`` are
reasoner-agnostic: they key on ``decision_id`` and go through the bundle
store. The failures an operator must be able to tell apart:

- no bundle → ``404 DECISION_NOT_FOUND`` (both routes);
- a bundle older than schema version 2 → lookup returns it as stored, with
  its ``schema_version``; replay answers ``409 BUNDLE_SCHEMA_UNSUPPORTED``;
- a bundle whose reasoner is not registered here → lookup still works (it
  needs nothing from the reasoner); replay answers
  ``409 REASONER_NOT_COMPOSED``;
- a version-2 row that no longer validates → ``500 BUNDLE_UNREADABLE``
  (both routes).

The lifespan's ``build_framework_services`` is replaced by a fake whose store
answers on demand; no infrastructure is touched and no plugin is composed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.audit.bundle import build_bundle
from app.audit.store import BundleStore, StoredBundle
from app.enforcement.resolver import resolve_decision, resolve_gate_output
from app.main import create_app
from core.actions import DecisionAction
from core.bundle import DecisionBundle, GateRecord
from core.exceptions import BundleSchemaUnsupportedError, UnregisteredReasonerError
from core.gate import EvidenceRef, GateInput, GateOutput, GateVerdict
from tests._stub_reasoner.registry import STUB_REGISTRATION

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
        "app.main.build_framework_services",
        lambda settings, plugins, gate_plugins: framework,
    )
    return bundle_store


@pytest.fixture
def client(store: MagicMock) -> Iterator[TestClient]:
    with TestClient(create_app(plugins=[], gate_plugins=[])) as test_client:
        yield test_client


def _gated_bundle(gated_submission):
    output = GateOutput(
        gate_id="fake",
        deterministic=False,
        verdict=GateVerdict(
            gate_id="fake",
            permitted_actions=[DecisionAction.CHALLENGE],
            required_controls=[],
            confidence=0.9,
            escalate_to_human=False,
            escalation_reason=None,
        ),
        evidence=(EvidenceRef(source_id="doc", jurisdictions=frozenset({"REGION_A"})),),
        evidence_snapshot={"retrieval_query": "q", "results": [{"score": -4.3}]},
        extra={"rationale": "why"},
    )
    record = GateRecord(
        gate_id="fake",
        mandatory=False,
        status="ran",
        input=GateInput(gate_id="fake"),
        output=output,
        enforcement=resolve_gate_output(
            output, registration=STUB_REGISTRATION, decision_id=_DECISION_ID
        ),
    )
    return build_bundle(
        decision_id=_DECISION_ID,
        submission=gated_submission,
        idempotency_key="idem",
        ingestion_timestamp=gated_submission.timestamp,
        gates=[record],
        decision=resolve_decision(
            submission=gated_submission,
            records=[record],
            registration=STUB_REGISTRATION,
            bypass_check=None,
            decision_id=_DECISION_ID,
        ),
    )


def _stored(bundle_json: dict, schema_version: int = 2) -> StoredBundle:
    return StoredBundle(
        decision_id=_DECISION_ID,
        reasoner_id="stub-reasoner",
        schema_version=schema_version,
        payload=bundle_json,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/decisions/{id}
# ---------------------------------------------------------------------------


def test_get_decision_returns_404_envelope_when_bundle_is_missing(
    client: TestClient, store: MagicMock
):
    store.lookup.side_effect = KeyError(_DECISION_ID)
    response = client.get(f"/api/v1/decisions/{_DECISION_ID}")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["error_code"] == "DECISION_NOT_FOUND"
    assert _DECISION_ID in detail["message"]


def test_get_decision_returns_a_v2_bundle_with_the_gates_opaque_extension(
    client: TestClient, store: MagicMock, gated_submission
):
    bundle = _gated_bundle(gated_submission)
    store.lookup.return_value = _stored(json.loads(bundle.model_dump_json()))

    response = client.get(f"/api/v1/decisions/{_DECISION_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 2
    assert body["decision_action"] == "CHALLENGE"
    assert body["submission"]["mode"] == "gate"
    [gate] = body["gates"]
    assert (gate["gate_id"], gate["status"]) == ("fake", "ran")
    assert gate["output"]["evidence_snapshot"] == {
        "retrieval_query": "q",
        "results": [{"score": -4.3}],
    }
    assert gate["output"]["extra"] == {"rationale": "why"}
    assert gate["output"]["evidence"] == [
        {"source_id": "doc", "locator": None, "jurisdictions": ["REGION_A"]}
    ]
    for legacy in ("raw_event", "gate_input", "gate_output", "retrieval_results"):
        assert legacy not in body


def test_get_decision_returns_a_v1_bundle_as_stored(
    client: TestClient, store: MagicMock
):
    legacy = {"decision_id": _DECISION_ID, "raw_event": {"route": "FAST_PATH_ALLOW"}}
    store.lookup.return_value = _stored(legacy, schema_version=1)

    response = client.get(f"/api/v1/decisions/{_DECISION_ID}")

    assert response.status_code == 200
    assert response.json() == {**legacy, "schema_version": 1}


def test_get_decision_answers_500_for_a_v2_row_that_does_not_validate(
    client: TestClient, store: MagicMock
):
    store.lookup.return_value = _stored({"decision_id": _DECISION_ID, "gates": 3})
    response = client.get(f"/api/v1/decisions/{_DECISION_ID}")
    assert response.status_code == 500
    assert response.json()["detail"]["error_code"] == "BUNDLE_UNREADABLE"


def test_get_decision_never_needs_the_reasoner(client: TestClient, store: MagicMock):
    # Lookup reads the row; it never consults the registry, so it cannot
    # answer REASONER_NOT_COMPOSED.
    store.lookup.return_value = _stored({"x": 1}, schema_version=1)
    assert client.get(f"/api/v1/decisions/{_DECISION_ID}").status_code == 200
    store.replay.assert_not_called()


# ---------------------------------------------------------------------------
# POST /api/v1/decisions/{id}/replay
# ---------------------------------------------------------------------------


def test_replay_returns_404_envelope_when_bundle_is_missing(
    client: TestClient, store: MagicMock
):
    store.replay.side_effect = KeyError(_DECISION_ID)
    response = client.post(f"/api/v1/decisions/{_DECISION_ID}/replay")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "DECISION_NOT_FOUND"


def test_replay_returns_409_for_a_bundle_older_than_schema_version_2(
    client: TestClient, store: MagicMock
):
    store.replay.side_effect = BundleSchemaUnsupportedError(
        _DECISION_ID, 1, supported=2
    )
    response = client.post(f"/api/v1/decisions/{_DECISION_ID}/replay")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error_code"] == "BUNDLE_SCHEMA_UNSUPPORTED"
    assert detail["details"] == {"schema_version": 1, "supported_schema_version": 2}


def test_replay_returns_409_envelope_when_reasoner_is_not_composed(
    client: TestClient, store: MagicMock
):
    store.replay.side_effect = UnregisteredReasonerError("other-reasoner")
    response = client.post(f"/api/v1/decisions/{_DECISION_ID}/replay")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error_code"] == "REASONER_NOT_COMPOSED"
    assert detail["details"] == {"reasoner_id": "other-reasoner"}


def _validation_error() -> ValidationError:
    try:
        DecisionBundle.model_validate_json('{"gates": 3}')
    except ValidationError as err:
        return err
    raise AssertionError("the row was expected not to validate")


def test_replay_answers_500_for_a_v2_row_that_does_not_validate(
    client: TestClient, store: MagicMock
):
    store.replay.side_effect = _validation_error()
    response = client.post(f"/api/v1/decisions/{_DECISION_ID}/replay")
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["error_code"] == "BUNDLE_UNREADABLE"
    assert "schema version 2" in detail["message"]


def test_replay_reports_the_store_result(client: TestClient, store: MagicMock):
    store.replay.return_value = SimpleNamespace(
        decision_id=_DECISION_ID,
        original_action="HOLD",
        replayed_action="HOLD",
        actions_match=True,
        original_bypass_rule_holds=True,
        bypass_rule_holds=True,
        gates=(
            SimpleNamespace(
                gate_id="fake",
                status="skipped",
                original_action=None,
                replayed_action=None,
            ),
        ),
        matched=True,
    )

    response = client.post(f"/api/v1/decisions/{_DECISION_ID}/replay")

    assert response.status_code == 200
    assert response.json() == {
        "decision_id": _DECISION_ID,
        "original_action": "HOLD",
        "replayed_action": "HOLD",
        "actions_match": True,
        "original_bypass_rule_holds": True,
        "bypass_rule_holds": True,
        "gates": [
            {
                "gate_id": "fake",
                "status": "skipped",
                "original_action": None,
                "replayed_action": None,
            }
        ],
        "matched": True,
    }
    store.replay.assert_called_once_with(_DECISION_ID)
