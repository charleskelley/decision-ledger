"""Acceptance on the live stack: the stream mock registered over HTTP (DR-35).

Requires Docker services: PostgreSQL and Elasticsearch, with the mock's
corpus loaded under the id its binding names
(``make prepare-reasoners``, or ``make build-policy-index-mock-stream-reasoner``),
and ``OPENAI_API_KEY`` set: the RAG policy gate builds its LLM client when
the ledger starts, although this test calls no model. Run with:
make test-integration

The real ledger app and bootstrap, composing no reasoner plugin and only the
real RAG policy gate, whose asset root (``POLICY_RAG_GATE_ASSETS_ROOT``) is a
temporary copy of the mock's assets:

- a ``PUT`` with the operator key registers the mock (``201``, source
  ``api``): the gate admitted the binding from the asset root, its provider
  having described the loaded corpus;
- the mock's own service, its lifespan run in the test's event loop, decides
  a stream of transfers over HTTP with the mock's key. Every transfer falls
  in a band (checked offline first: a gated one would call the live model,
  which only ``@pytest.mark.evaluation`` tests may do), so each is recorded
  under the registration version the module computes, the advisory gate
  skipped, and replays matched; verify finds no evidence reference.

The mock's ``decisionledger.reasoner_registry`` row is deleted before and
after (a later ledger that composes the mock's plugin would refuse to start
beside it); its registration snapshots are kept, since the plugin and the
API registration share the version and bundles cite it. The decisions the
test wrote, and their replay logs, are deleted on teardown.
"""

from __future__ import annotations

import json
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.audit.schema import apply_canonical_schema
from app.auth import KEY_HEADER, KeyRing
from app.client import HttpClient
from app.gates import ReasonerScope
from app.main import create_app as create_ledger_app
from app.settings import FrameworkSettings
from core.actions import DecisionAction
from core.observation import canonical_registration, registration_version
from mock_stream_reasoner.app import create_app as create_mock_app
from mock_stream_reasoner.assembler import build_submission
from mock_stream_reasoner.events import TransferEvent
from mock_stream_reasoner.plugin import ASSETS_DIR
from mock_stream_reasoner.registry import MOCK_STREAM_REGISTRATION
from mock_stream_reasoner.replay import replay_events
from mock_stream_reasoner.scorer import score_transfer
from mock_stream_reasoner.settings import MockStreamSettings
from mock_stream_reasoner.source import ALLOWED_LATENESS
from mock_stream_reasoner.state import WINDOW, AccountWindows
from tests._memory_ledger import OPERATOR_KEY

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator
    from pathlib import Path

    from app.client import LedgerClient

pytest.importorskip("policy_rag_gate")

pytestmark = pytest.mark.integration

_PG_DSN = FrameworkSettings().postgres_dsn

REASONER_ID = MOCK_STREAM_REGISTRATION.reasoner_id
VERSION = registration_version(MOCK_STREAM_REGISTRATION)
#: The mock's key on the ledger (a test value).
MOCK_KEY = "mock-stream-test-key"
LEDGER_URL = "http://ledger.test"
SERVICE_URL = "http://mock.test"


def _forget_api_registration() -> None:
    """Delete the mock's API registration row (never its snapshots)."""
    with psycopg.connect(_PG_DSN, autocommit=True) as conn:
        apply_canonical_schema(conn)
        conn.execute(
            "DELETE FROM decisionledger.reasoner_registry WHERE reasoner_id = %s",
            (REASONER_ID,),
        )


@pytest.fixture
def openai_key() -> None:
    """Skip, before touching the database, when the gate cannot be built."""
    from policy_rag_gate.settings import PolicyRagGateSettings

    if not PolicyRagGateSettings().openai_api_key.strip():
        pytest.skip(
            "OPENAI_API_KEY is not set: the RAG policy gate builds its LLM "
            "client at startup (this test calls no model)"
        )


@pytest.fixture
def unregistered(openai_key: None) -> Iterator[None]:
    """No API registration of the mock before the test, and none after."""
    _forget_api_registration()
    try:
        yield
    finally:
        _forget_api_registration()


@pytest.fixture
def written() -> Iterator[list[str]]:
    """Event ids a test published; their decisions and logs go on teardown."""
    event_ids: list[str] = []
    yield event_ids
    if not event_ids:
        return
    with psycopg.connect(_PG_DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT decision_id::text FROM decisionledger.decision_bundles "
            "WHERE reasoner_id = %s "
            "AND bundle -> 'submission' ->> 'event_id' = ANY(%s)",
            (REASONER_ID, event_ids),
        )
        decision_ids = [str(row[0]) for row in cur.fetchall()]
        if not decision_ids:
            return
        for table in ("replay_logs", "decision_resolution_attempts"):
            cur.execute(
                f"DELETE FROM decisionledger.{table} "  # noqa: S608 (fixed names)
                "WHERE decision_id = ANY(%s::uuid[])",
                (decision_ids,),
            )
        cur.execute(
            "DELETE FROM decisionledger.decision_bundles "
            "WHERE decision_id = ANY(%s::uuid[])",
            (decision_ids,),
        )


@pytest.fixture
def policy_gate_plugin(
    openai_key: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Any:
    """The real RAG policy gate, its asset root a copy of the mock's assets."""
    from policy_rag_gate.plugin import PolicyGatePlugin

    shutil.copytree(ASSETS_DIR, tmp_path / REASONER_ID)
    monkeypatch.setenv("POLICY_RAG_GATE_ASSETS_ROOT", str(tmp_path))
    # A fresh plugin: the entry point's PLUGIN caches the settings it read.
    plugin = PolicyGatePlugin()
    assert plugin.settings.assets_root_path == tmp_path
    return plugin


@pytest.fixture
def live_ledger(unregistered: None, policy_gate_plugin: Any) -> Iterator[TestClient]:
    """The ledger with the real bootstrap, no reasoner plugin, only the gate."""
    app = create_ledger_app(
        plugins=[],
        gate_plugins=[policy_gate_plugin],
        keyring=KeyRing({REASONER_ID: [MOCK_KEY]}, OPERATOR_KEY),
    )
    with TestClient(app) as client:
        yield client


@asynccontextmanager
async def _mock_service(
    client_factory: Callable[[MockStreamSettings], LedgerClient],
) -> AsyncIterator[httpx.AsyncClient]:
    """The mock's service, its stream and consumer running in this event loop."""
    settings = MockStreamSettings(
        ledger_url=LEDGER_URL,
        ledger_key=SecretStr(MOCK_KEY),
        window_seconds=int(WINDOW.total_seconds()),
        allowed_lateness_seconds=int(ALLOWED_LATENESS.total_seconds()),
    )
    service = create_mock_app(settings, client_factory=client_factory)
    async with (
        service.router.lifespan_context(service),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service), base_url=SERVICE_URL
        ) as http,
    ):
        yield http


def _transfers(run: str) -> list[TransferEvent]:
    """One account's transfers, twenty minutes apart (none in another's window)."""
    start = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    amounts = [80.0, 90.0, 100.0, 95.0, 6000.0]
    return [
        TransferEvent(
            event_id=f"evt-live-{run}-{n}",
            timestamp=start + timedelta(minutes=20 * n),
            account_id=f"acct-live-{run}",
            amount=amount,
        )
        for n, amount in enumerate(amounts)
    ]


async def test_a_mock_registered_over_http_records_on_the_live_stack_and_replays(
    live_ledger: TestClient, written: list[str]
):
    framework = live_ledger.app.state.framework
    assert framework.reasoners == ()
    policy = framework.gates["policy"].gate
    assert isinstance(policy, ReasonerScope)
    assert not policy.serves(REASONER_ID)

    response = live_ledger.put(
        f"/api/v1/reasoners/{REASONER_ID}",
        content=json.dumps(canonical_registration(MOCK_STREAM_REGISTRATION)),
        headers={KEY_HEADER: OPERATOR_KEY},
    )
    assert response.status_code == 201, response.text
    view = response.json()
    assert (view["source"], view["registered_by"], view["registration_version"]) == (
        "api",
        "operator",
        VERSION,
    )
    assert policy.serves(REASONER_ID)  # admitted from the asset root

    # Every transfer must fall in a band: a gated one would call the model.
    events = _transfers(uuid.uuid4().hex[:8])
    windows = AccountWindows()
    planned = []
    for event in events:
        score = score_transfer(windows.observe(event))
        submission = build_submission(event, score)
        planned.append(
            (
                submission.mode,
                submission.proposed_action,
                submission.bypass.rule_id if submission.bypass is not None else None,
                score.features.thin_history,
            )
        )
    allow = ("record", DecisionAction.ALLOW, "steady-activity")
    assert planned == [
        (*allow, True),
        (*allow, True),
        (*allow, True),
        (*allow, False),
        ("record", DecisionAction.BLOCK, "activity-surge", False),
    ]
    written.extend(event.event_id for event in events)

    def ledger_client() -> HttpClient:
        return HttpClient(
            LEDGER_URL,
            api_key=MOCK_KEY,
            transport=httpx.ASGITransport(app=live_ledger.app),
        )

    async with _mock_service(lambda _settings: ledger_client()) as service:
        summary = await replay_events(
            [event.payload() for event in events],
            client=service,
            timeout=120.0,
            poll=0.05,
        )

    assert summary.outcomes == {"accepted": len(events)}
    assert summary.dead_letters == []
    decided = {d["event_id"]: d for d in summary.decisions}
    assert [
        (
            decided[event.event_id]["mode"],
            decided[event.event_id]["decision_action"],
            decided[event.event_id]["bypass_rule_id"],
            decided[event.event_id]["decision_level_rule"],
        )
        for event in events
    ] == [(mode, action.value, rule, None) for mode, action, rule, _ in planned]

    async with ledger_client() as ledger:
        served = await ledger.reasoner(REASONER_ID)
        assert (served.source, served.registration_version) == ("api", VERSION)
        for event in events:
            decision_id = decided[event.event_id]["decision_id"]
            bundle = await ledger.get_bundle(decision_id)
            assert bundle.registration_version == VERSION
            gate = bundle.gate("policy")
            assert gate is not None
            assert gate.status == "skipped"  # advisory, on a record: no model call
            replay = await ledger.replay(decision_id)
            assert replay.matched, event.event_id
            assert replay.registration_version == VERSION
        verification = await ledger.verify(decided[events[-1].event_id]["decision_id"])
        assert verification.refs == []
