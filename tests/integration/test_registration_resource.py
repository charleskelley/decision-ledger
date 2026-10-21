"""Integration tests for registrations written over HTTP, on Postgres (DR-33).

Requires PostgreSQL. Run with: make test-integration.

The real app and bootstrap, the stub reasoner as the declared tenant, and
the scripted fake gate; ``other-reasoner`` (which the test key ring holds a
key for) is registered over the API:

- a ``PUT`` stores the registration's snapshot and its
  ``decisionledger.reasoner_registry`` row in one statement, and a restarted
  ledger serves it again, as ``api``, with the times it was stored with;
- the reasoner decides under that version, and after a replacement its
  earlier decision replays against the snapshot it cites;
- a refused ``PUT`` stores nothing;
- a reasoner a plugin registers and the table holds too refuses startup;
- ``read_registry`` composes what a ledger would serve, read only.

Every row a test writes is deleted on teardown, since the dev database is
shared and API registrations are composed by every later start.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.audit.registrations import ApiRegistrations
from app.auth import KEY_HEADER
from app.bootstrap import read_registry
from app.main import create_app
from app.settings import FrameworkSettings
from core.exceptions import RegistrationError
from core.observation import (
    GateBinding,
    GateContext,
    canonical_registration,
    registration_version,
)
from tests._fake_gate.plugin import FakeGatePlugin
from tests._memory_ledger import OPERATOR_KEY, OTHER_KEY, TEST_KEYRING
from tests._stub_reasoner.assembler import build_submission
from tests._stub_reasoner.plugin import PLUGIN
from tests._stub_reasoner.registry import STUB_REGISTRATION

if TYPE_CHECKING:
    from collections.abc import Iterator

    from core.observation import ReasonerContext, ReasonerRegistration
    from tests._stub_reasoner.events import StubEvent

pytestmark = pytest.mark.integration

_PG_DSN = FrameworkSettings().postgres_dsn

#: The stub's registration as another reasoner, which holds a test key.
OTHER = STUB_REGISTRATION.model_copy(
    update={"reasoner_id": "other-reasoner", "reasoner_name": "Other Reasoner"}
)


def _delete(reasoner_ids: list[str], decision_ids: list[str]) -> None:
    with psycopg.connect(_PG_DSN, autocommit=True) as conn, conn.cursor() as cur:
        if decision_ids:
            cur.execute(
                "DELETE FROM decisionledger.replay_logs "
                "WHERE decision_id = ANY(%s::uuid[])",
                (decision_ids,),
            )
            cur.execute(
                "DELETE FROM decisionledger.decision_bundles "
                "WHERE decision_id = ANY(%s::uuid[])",
                (decision_ids,),
            )
        cur.execute(
            "DELETE FROM decisionledger.reasoner_registry WHERE reasoner_id = ANY(%s)",
            (reasoner_ids,),
        )
        cur.execute(
            "DELETE FROM decisionledger.reasoner_registrations "
            "WHERE reasoner_id = ANY(%s)",
            (reasoner_ids,),
        )


@pytest.fixture
def written() -> Iterator[list[str]]:
    """Decision ids a test wrote; they and other-reasoner's rows go on teardown."""
    _delete(["other-reasoner"], [])
    decision_ids: list[str] = []
    yield decision_ids
    _delete(["other-reasoner"], decision_ids)


def _app() -> Any:
    return create_app(
        plugins=[PLUGIN], gate_plugins=[FakeGatePlugin()], keyring=TEST_KEYRING
    )


def _put(client: TestClient, registration: ReasonerRegistration):
    return client.put(
        f"/api/v1/reasoners/{registration.reasoner_id}",
        content=json.dumps(canonical_registration(registration)),
        headers={KEY_HEADER: OPERATOR_KEY},
    )


def _rows(reasoner_id: str) -> tuple[list[tuple[Any, ...]], list[str]]:
    with psycopg.connect(_PG_DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT registration_version, source, registered_by "
            "FROM decisionledger.reasoner_registry WHERE reasoner_id = %s",
            (reasoner_id,),
        )
        rows = cur.fetchall()
        cur.execute(
            "SELECT registration_version FROM decisionledger.reasoner_registrations "
            "WHERE reasoner_id = %s ORDER BY first_registered_at",
            (reasoner_id,),
        )
        versions = [str(row[0]) for row in cur.fetchall()]
    return rows, versions


def test_a_registration_written_over_http_is_stored_and_served_after_a_restart(
    written: list[str],
):
    with TestClient(_app()) as client:
        response = _put(client, OTHER)
    assert response.status_code == 201, response.text
    created = response.json()
    version = registration_version(OTHER)

    rows, versions = _rows("other-reasoner")
    assert rows == [(version, "api", "operator")]
    assert versions == [version]

    with TestClient(_app()) as restarted:
        served = restarted.get(
            "/api/v1/reasoners/other-reasoner", headers={KEY_HEADER: OTHER_KEY}
        ).json()
    assert served == created


def test_a_reasoner_registered_over_http_decides_and_replays_against_its_version(
    written: list[str], stub_event: StubEvent, reasoner_context: ReasonerContext
):
    context = reasoner_context.model_copy(
        update={"reasoner_id": "other-reasoner", "label_value": 0.55}
    )
    submission = build_submission(
        stub_event.model_copy(update={"event_id": f"evt-{uuid.uuid4()}"}),
        context,
        gate_contexts=[GateContext(gate_id="fake")],
        registration=OTHER,
    ).model_copy(update={"entity_id": uuid.uuid4()})
    with TestClient(_app()) as client:
        first = _put(client, OTHER).json()["registration_version"]
        response = client.post(
            "/api/v1/decisions",
            content=submission.model_dump_json(),
            headers={KEY_HEADER: OTHER_KEY},
        )
        assert response.status_code == 201, response.text
        receipt = response.json()
        written.append(receipt["decision_id"])
        assert receipt["registration_version"] == first

        replaced = _put(client, OTHER.model_copy(update={"novel_entity_feature": None}))
        assert replaced.status_code == 200
        assert replaced.json()["registration_version"] != first

    with TestClient(_app()) as restarted:
        replay = restarted.post(
            f"/api/v1/decisions/{receipt['decision_id']}/replay",
            headers={KEY_HEADER: OTHER_KEY},
        ).json()
    assert replay["matched"] is True
    assert replay["registration_version"] == first
    _, versions = _rows("other-reasoner")
    assert len(versions) == 2


def test_a_refused_registration_stores_nothing(written: list[str]):
    refused = OTHER.model_copy(
        update={"gates": (GateBinding(gate_id="absent-gate", mandatory=True),)}
    )
    with TestClient(_app()) as client:
        response = _put(client, refused)

    assert response.status_code == 422
    assert response.json()["code"] == "GATE_BINDING_REFUSED"
    assert _rows("other-reasoner") == ([], [])


def test_a_plugin_reasoner_also_registered_over_the_api_refuses_startup():
    """The table is the source of truth on restart: it may not shadow a plugin."""
    with psycopg.connect(_PG_DSN, autocommit=True) as conn:
        ApiRegistrations(conn).save(STUB_REGISTRATION, registered_by="operator")
    try:
        with (
            pytest.raises(RegistrationError, match="registered over the API and by"),
            TestClient(_app()),
        ):
            pass
    finally:
        with psycopg.connect(_PG_DSN, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM decisionledger.reasoner_registry "
                "WHERE reasoner_id = 'stub-reasoner'"
            )


def test_a_registration_reloaded_differently_is_served_under_its_own_version(
    written: list[str],
):
    """jsonb stores 2e16 as an integer: the reloaded registration's version wins.

    The write route refuses such numbers; a row stored another way is served
    under the version intake stamps on its decisions, never the stale one.
    """
    rewritten = OTHER.model_copy(
        update={"gates": (GateBinding(gate_id="fake", config={"cap": 2e16}),)}
    )
    with psycopg.connect(_PG_DSN, autocommit=True) as conn:
        stored = ApiRegistrations(conn).save(rewritten, registered_by="operator")
        [entry] = [
            e
            for e in ApiRegistrations(conn).load()
            if e.reasoner_id == "other-reasoner"
        ]

    assert entry.registration.gates[0].config == {"cap": 20000000000000000}
    assert entry.version == registration_version(entry.registration)
    assert entry.version != stored.version


def test_read_registry_composes_what_a_ledger_would_serve(written: list[str]):
    with psycopg.connect(_PG_DSN, autocommit=True) as conn:
        ApiRegistrations(conn).save(OTHER, registered_by="operator")
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE

    registry = read_registry(FrameworkSettings(), [PLUGIN])

    assert registry.entry("other-reasoner").source == "api"
    assert registry.entry("stub-reasoner").source.startswith("plugin:")
    registry.register(
        OTHER.model_copy(update={"reasoner_name": "Changed"}), registered_by="x"
    )
    rows, _ = _rows("other-reasoner")
    assert rows == [(registration_version(OTHER), "api", "operator")]
