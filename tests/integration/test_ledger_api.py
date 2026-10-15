"""Integration tests for the ledger's HTTP surface on Postgres (DR-31).

Requires PostgreSQL. Run with: make test-integration (smoke subset:
make test-smoke).

The real app and bootstrap, the stub reasoner as the tenant, and the
scripted fake gate — Postgres is the only infrastructure:

- (smoke) a record through ``POST /api/v1/decisions`` with the stub's key is
  written, looked up, and replays to a match;
- (smoke) a gated submission runs the fake gate;
- (smoke) another reasoner's key is refused with 403 and nothing is written;
- a batch is bulk-inserted and the subject's timeline lists it, newest first;
- the registrations resource serves the stored snapshot a bundle cites, and
  a decision replays from it after its reasoner left the deployment;
- the operator resolves a held decision; the attempt is in the journal.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.auth import KEY_HEADER
from app.main import create_app
from app.settings import FrameworkSettings
from core.observation import GateContext, registration_version
from tests._fake_gate.plugin import FakeGatePlugin
from tests._memory_ledger import OPERATOR_KEY, OTHER_KEY, STUB_KEY, TEST_KEYRING
from tests._stub_reasoner.plugin import PLUGIN
from tests._stub_reasoner.registry import STUB_REGISTRATION

if TYPE_CHECKING:
    from collections.abc import Iterator

    from core.submission import Submission

pytestmark = pytest.mark.integration

_PG_DSN = FrameworkSettings().postgres_dsn


@pytest.fixture
def cleanup() -> Iterator[list[str]]:
    """Decision ids a test wrote; deleted (with their logs) on teardown."""
    ids: list[str] = []
    yield ids
    if ids:
        with psycopg.connect(_PG_DSN, autocommit=True) as conn, conn.cursor() as cur:
            for table in ("replay_logs", "decision_resolution_attempts"):
                cur.execute(
                    f"DELETE FROM decisionledger.{table} "  # noqa: S608 (fixed names)
                    "WHERE decision_id = ANY(%s::uuid[])",
                    (ids,),
                )
            cur.execute(
                "DELETE FROM decisionledger.decision_bundles "
                "WHERE decision_id = ANY(%s::uuid[])",
                (ids,),
            )


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(
        plugins=[PLUGIN], gate_plugins=[FakeGatePlugin()], keyring=TEST_KEYRING
    )
    with TestClient(app) as test_client:
        yield test_client


def _unique(submission: Submission) -> Submission:
    """The submission about a fresh subject, with a fresh event id."""
    return submission.model_copy(
        update={"event_id": f"evt-{uuid.uuid4()}", "entity_id": uuid.uuid4()}
    )


def _post(client: TestClient, path: str, body: str, key: str = STUB_KEY):
    return client.post(path, content=body, headers={KEY_HEADER: key})


def _get(client: TestClient, path: str, key: str = STUB_KEY, **kw: Any):
    return client.get(path, headers={KEY_HEADER: key}, **kw)


def _count(event_id: str) -> int:
    with psycopg.connect(_PG_DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM decisionledger.decision_bundles "
            "WHERE bundle -> 'submission' ->> 'event_id' = %s",
            (event_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.smoke
def test_a_record_is_written_looked_up_and_replayed_over_http(
    client: TestClient, cleanup: list[str], submission
):
    record = _unique(submission)
    response = _post(client, "/api/v1/decisions", record.model_dump_json())
    assert response.status_code == 201, response.text
    receipt = response.json()
    cleanup.append(receipt["decision_id"])
    assert receipt["decision_action"] == "ALLOW"
    assert receipt["registration_version"] == registration_version(STUB_REGISTRATION)

    bundle = _get(client, response.headers["location"]).json()
    assert bundle["submission"]["event_id"] == record.event_id
    replay = _post(client, f"/api/v1/decisions/{receipt['decision_id']}/replay", "")
    assert replay.json()["matched"] is True


@pytest.mark.smoke
def test_a_gated_submission_runs_its_gate_over_http(
    client: TestClient, cleanup: list[str], gated_submission
):
    gated = _unique(gated_submission).model_copy(
        update={
            "gate_contexts": (
                GateContext(
                    gate_id="fake", gate_config={"permitted_actions": ["CHALLENGE"]}
                ),
            )
        }
    )
    response = _post(client, "/api/v1/decisions", gated.model_dump_json())
    assert response.status_code == 201, response.text
    receipt = response.json()
    cleanup.append(receipt["decision_id"])
    assert receipt["mode"] == "gate"
    assert receipt["decision_action"] == "CHALLENGE"
    assert receipt["gates"][0]["status"] == "ran"


@pytest.mark.smoke
def test_another_reasoners_key_is_refused_and_nothing_is_written(
    client: TestClient, submission
):
    record = _unique(submission)
    response = _post(client, "/api/v1/decisions", record.model_dump_json(), OTHER_KEY)
    assert response.status_code == 403
    assert response.json()["code"] == "REASONER_MISMATCH"
    assert _count(record.event_id) == 0


def test_a_batch_is_bulk_inserted_and_listed_on_the_subjects_timeline(
    client: TestClient, cleanup: list[str], submission
):
    entity_id = uuid.uuid4()
    lines = [
        submission.model_copy(
            update={"event_id": f"evt-{uuid.uuid4()}", "entity_id": entity_id}
        )
        for _ in range(25)
    ]
    body = "".join(f"{line.model_dump_json()}\n" for line in lines)

    response = _post(client, "/api/v1/decisions:batch", body)

    assert response.status_code == 200, response.text
    result = response.json()
    cleanup.extend(r["decision_id"] for r in result["results"])
    assert (result["recorded"], result["rejected"]) == (25, 0)
    timeline = _get(
        client, f"/api/v1/entities/{entity_id}/decisions", params={"limit": 100}
    ).json()["decisions"]
    assert {d["decision_id"] for d in timeline} == {
        r["decision_id"] for r in result["results"]
    }
    created = [d["created_at"] for d in timeline]
    assert created == sorted(created, reverse=True)


def test_a_decision_replays_from_its_snapshot_after_its_reasoner_left(
    client: TestClient, cleanup: list[str], submission
):
    receipt = _post(
        client, "/api/v1/decisions", _unique(submission).model_dump_json()
    ).json()
    cleanup.append(receipt["decision_id"])
    version = receipt["registration_version"]
    snapshot = _get(
        client, f"/api/v1/reasoners/stub-reasoner/registrations/{version}"
    ).json()
    assert snapshot["registration"]["reasoner_id"] == "stub-reasoner"

    # A deployment that no longer composes the stub still replays its decision.
    bare = create_app(plugins=[], gate_plugins=[], keyring=TEST_KEYRING)
    with TestClient(bare) as other:
        replay = _post(
            other,
            f"/api/v1/decisions/{receipt['decision_id']}/replay",
            "",
            OPERATOR_KEY,
        )
    assert replay.status_code == 200, replay.text
    assert replay.json()["matched"] is True
    assert replay.json()["registration_version"] == version


def test_the_operator_resolves_a_held_decision(
    client: TestClient, cleanup: list[str], submission
):
    context = submission.reasoner_context.model_copy(update={"label_value": 0.5})
    held = _unique(submission).model_copy(update={"reasoner_context": context})
    receipt = _post(client, "/api/v1/decisions", held.model_dump_json()).json()
    cleanup.append(receipt["decision_id"])
    assert receipt["decision_action"] == "HOLD"

    resolution = (
        '{"resolver_id": "analyst-7", "resolution_action": "ALLOW", "note": "verified"}'
    )
    response = _post(
        client,
        f"/api/v1/decisions/{receipt['decision_id']}/resolutions",
        resolution,
        OPERATOR_KEY,
    )

    assert response.status_code == 201, response.text
    assert response.json()["realized_action"] == "ALLOW"
    with psycopg.connect(_PG_DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT resolver_kind, resolution_action FROM "
            "decisionledger.decision_resolution_attempts WHERE decision_id = %s",
            (receipt["decision_id"],),
        )
        assert cur.fetchall() == [("HUMAN", "ALLOW")]


def test_a_timeline_pages_through_decisions_of_one_instant_on_postgres(
    client: TestClient, cleanup: list[str], submission
):
    """The cursor is (created_at, decision_id), as the order is."""
    entity_id = uuid.uuid4()
    ids = []
    for n in range(3):
        receipt = _post(
            client,
            "/api/v1/decisions",
            submission.model_copy(
                update={"event_id": f"evt-{uuid.uuid4()}-{n}", "entity_id": entity_id}
            ).model_dump_json(),
        ).json()
        cleanup.append(receipt["decision_id"])
        ids.append(receipt["decision_id"])
    with psycopg.connect(_PG_DSN, autocommit=True) as conn:
        conn.execute(
            "UPDATE decisionledger.decision_bundles SET created_at = "
            "'2026-10-14T12:00:00Z' WHERE decision_id = ANY(%s::uuid[])",
            (ids,),
        )

    seen: list[str] = []
    params: dict[str, Any] = {"limit": 1}
    for _ in range(4):
        page = _get(client, f"/api/v1/entities/{entity_id}/decisions", params=params)
        decisions = page.json()["decisions"]
        if not decisions:
            break
        last = decisions[-1]
        seen.append(last["decision_id"])
        params = {
            "limit": 1,
            "before": last["created_at"],
            "before_id": last["decision_id"],
        }
    assert sorted(seen) == sorted(ids)
    assert len(seen) == 3


def test_any_uuid_spelling_is_looked_up_on_postgres(
    client: TestClient, cleanup: list[str], submission
):
    decision_id = _post(
        client, "/api/v1/decisions", _unique(submission).model_dump_json()
    ).json()["decision_id"]
    cleanup.append(decision_id)
    for spelling in (f"urn:uuid:{decision_id}", decision_id.replace("-", "")):
        response = _get(client, f"/api/v1/decisions/{spelling}")
        assert response.status_code == 200, (spelling, response.text)
    response = _get(client, f"/api/v1/entities/urn:uuid:{uuid.uuid4()}/decisions")
    assert response.status_code == 200
