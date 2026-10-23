"""Acceptance: a ledger that never imports the stream mock serves it (DR-35).

The plan's Docker-free acceptance for the real-time event scorer pattern. The
ledger composes no reasoner plugin (the mock's is excluded) and registers the
mock by its registration file or, separately, by a ``PUT`` with the operator
key; the mock's own service runs its lifespan (the stream and its consumer)
in the test's event loop and is posted to through ``httpx.ASGITransport``,
and a synthetic stream is replayed into it (``replay_events``):

- the stream drops re-deliveries and dead-letters the late and the malformed
  event; every transfer it accepts is decided, in both modes: a transfer in
  a band is recorded under that band's bypass rule (a thin history does not
  hold a record), any other is gated;
- a gated transfer of an account with a thin history (a fresh account's
  large first transfer) is held by Tier 3 whatever the policy double
  permits; every other gated transfer gets the double's verdict;
- every bundle carries the registration version the module computes
  (``registration_version(MOCK_STREAM_REGISTRATION)``), so the file, the
  API, and the module agree, and replays of a recorded, a gated, and a
  Tier 3 decision match;
- by PUT: 404 before, 201 then 200 unchanged, operator only, the gate
  admits the binding, and the mock decides over HTTP with its own key; a
  binding the gate refuses registers nothing.

The policy gate is the member's scripted double
(``mock_stream_reasoner.testing``): no Postgres, Elasticsearch, or model.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.auth import KEY_HEADER, KeyRing
from app.client import HttpClient, LocalClient
from app.enforcement.resolver import TIER3_NOVEL_ENTITY
from app.main import create_app as create_ledger_app
from core.observation import canonical_registration, registration_version
from mock_stream_reasoner.app import create_app as create_mock_app
from mock_stream_reasoner.registry import (
    MOCK_STREAM_CORPUS_ID,
    MOCK_STREAM_CORPUS_TIER,
    MOCK_STREAM_REGISTRATION,
)
from mock_stream_reasoner.replay import replay_events, synthetic_stream
from mock_stream_reasoner.settings import MockStreamSettings
from mock_stream_reasoner.source import ALLOWED_LATENESS
from mock_stream_reasoner.state import WINDOW
from mock_stream_reasoner.testing import ScriptedPolicyGate, ScriptedPolicyGatePlugin
from tests._memory_ledger import OPERATOR_KEY, memory_framework

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator
    from datetime import datetime
    from pathlib import Path

    from app.client import LedgerClient
    from app.plugin import FrameworkServices
    from mock_stream_reasoner.replay import ReplaySummary

REASONER_ID = MOCK_STREAM_REGISTRATION.reasoner_id
#: The version every source of the registration must serve.
VERSION = registration_version(MOCK_STREAM_REGISTRATION)
#: The mock's key on the ledger (a test value).
MOCK_KEY = "mock-stream-test-key"
KEYRING = KeyRing({REASONER_ID: [MOCK_KEY]}, OPERATOR_KEY)
LEDGER_URL = "http://ledger.test"
SERVICE_URL = "http://mock.test"
#: What a synthetic stream carries besides transfers: three re-deliveries,
#: one late event, and one malformed payload.
STREAM_FAULTS = 5
#: Events of the stream replayed over HTTP; it holds fresh accounts' large
#: first transfers, the burst, and every fault.
PUT_STREAM_COUNT = 100


def _mock_settings() -> MockStreamSettings:
    """The mock's settings, every value given (no environment is read)."""
    return MockStreamSettings(
        ledger_url=LEDGER_URL,
        ledger_key=SecretStr(MOCK_KEY),
        window_seconds=int(WINDOW.total_seconds()),
        allowed_lateness_seconds=int(ALLOWED_LATENESS.total_seconds()),
    )


@asynccontextmanager
async def _mock_service(
    client_factory: Callable[[MockStreamSettings], LedgerClient],
) -> AsyncIterator[httpx.AsyncClient]:
    """The mock's service, its stream and consumer running in this event loop."""
    service = create_mock_app(_mock_settings(), client_factory=client_factory)
    async with (
        service.router.lifespan_context(service),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service), base_url=SERVICE_URL
        ) as http,
    ):
        yield http


async def _replay(
    service: httpx.AsyncClient, payloads: list[dict[str, Any]]
) -> ReplaySummary:
    return await replay_events(payloads, client=service, timeout=60.0, poll=0.01)


def _checked(
    summary: ReplaySummary, payloads: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Check what the service made of a synthetic stream; one decision per kind.

    Returns:
        A recorded ALLOW, a recorded BLOCK, a decision the double's verdict
        decided, and a Tier 3 hold, by kind.
    """
    transfers = len(payloads) - STREAM_FAULTS
    assert summary.outcomes == {
        "accepted": transfers,
        "dead_lettered:late": 1,
        "dead_lettered:malformed": 1,
        "duplicate": 3,
    }
    # Nothing the stream accepted was refused by the ledger or lost.
    assert sorted(letter["reason"] for letter in summary.dead_letters) == [
        "late",
        "malformed",
    ]
    decisions = summary.decisions
    assert len(decisions) == transfers
    assert len({d["event_id"] for d in decisions}) == transfers

    records = [d for d in decisions if d["mode"] == "record"]
    gated = [d for d in decisions if d["mode"] == "gate"]
    assert len(records) + len(gated) == transfers
    # A record is decided as its band says, whatever the account's history.
    assert {(d["bypass_rule_id"], d["decision_action"]) for d in records} == {
        ("steady-activity", "ALLOW"),
        ("activity-surge", "BLOCK"),
    }
    assert all(d["decision_level_rule"] is None for d in records)
    assert any(d["thin_history"] for d in records)
    assert all(d["bypass_rule_id"] is None for d in gated)
    # A gated transfer of a thin account is held by Tier 3, whatever the
    # double permits: the fresh accounts' large first transfers.
    held = [d for d in gated if d["thin_history"]]
    assert held
    assert all(
        (d["decision_action"], d["decision_level_rule"]) == ("HOLD", TIER3_NOVEL_ENTITY)
        and "-fresh-" in d["account_id"]
        for d in held
    )
    # Any other gated transfer is decided by the double's verdict.
    verdicts = [d for d in gated if not d["thin_history"]]
    assert verdicts
    assert all(
        (d["decision_action"], d["decision_level_rule"]) == ("CHALLENGE", None)
        for d in verdicts
    )
    return {
        "recorded ALLOW": next(d for d in records if d["decision_action"] == "ALLOW"),
        "recorded BLOCK": next(d for d in records if d["decision_action"] == "BLOCK"),
        "gated": verdicts[0],
        "held by Tier 3": held[0],
    }


async def test_the_ledger_registers_the_mock_by_file_and_it_decides_holds_and_replays(
    registration_file: Path, t0: datetime
):
    gate = ScriptedPolicyGate()
    framework = memory_framework(
        (), [ScriptedPolicyGatePlugin(gate)], registration_file=registration_file
    )
    assert framework.reasoners == ()  # the mock's plugin is not composed
    entry = framework.registry.entry(REASONER_ID)
    assert entry.source == f"file:{registration_file}"
    assert entry.version == VERSION
    # The double admitted the file's binding when the ledger started.
    assert [reasoner_id for reasoner_id, _ in gate.bindings] == [REASONER_ID]

    payloads = synthetic_stream(200, run_tag="byfile", start=t0)
    async with _mock_service(lambda _settings: LocalClient(framework)) as service:
        summary = await _replay(service, payloads)

    picks = _checked(summary, payloads)
    # Only gated transfers asked the advisory gate.
    assert len(gate.requests) == sum(d["mode"] == "gate" for d in summary.decisions)
    ledger = LocalClient(framework)
    for decision in summary.decisions:
        bundle = await ledger.get_bundle(decision["decision_id"])
        assert bundle.submission.event_id == decision["event_id"]
        assert bundle.registration_version == VERSION
    for kind, decision in picks.items():
        replay = await ledger.replay(decision["decision_id"])
        assert replay.matched, kind
        assert replay.registration_version == VERSION


@dataclass
class LedgerService:
    """The ledger's app over in-memory services, no reasoner plugin composed."""

    client: TestClient
    framework: FrameworkServices
    gate: ScriptedPolicyGate

    def put(self, *, key: str) -> httpx.Response:
        return self.client.put(
            f"/api/v1/reasoners/{REASONER_ID}",
            content=json.dumps(canonical_registration(MOCK_STREAM_REGISTRATION)),
            headers={KEY_HEADER: key},
        )

    def get(self, *, key: str = OPERATOR_KEY) -> httpx.Response:
        return self.client.get(
            f"/api/v1/reasoners/{REASONER_ID}", headers={KEY_HEADER: key}
        )

    def http_client(self) -> HttpClient:
        """A ledger client with the mock's own key, over the app (no network)."""
        return HttpClient(
            LEDGER_URL,
            api_key=MOCK_KEY,
            transport=httpx.ASGITransport(app=self.client.app),
        )


@contextmanager
def _ledger_service(
    monkeypatch: pytest.MonkeyPatch, gate: ScriptedPolicyGate
) -> Iterator[LedgerService]:
    """``app.main.create_app`` with no reasoner plugin and only the double.

    The bootstrap is replaced by in-memory services holding no registration;
    the routes, auth, admission, enforcement, and replay are the production
    code.
    """
    plugin = ScriptedPolicyGatePlugin(gate)
    framework = memory_framework((), [plugin])
    monkeypatch.setattr(
        "app.main.build_framework_services",
        lambda settings, plugins, gate_plugins: framework,
    )
    app = create_ledger_app(plugins=[], gate_plugins=[plugin], keyring=KEYRING)
    with TestClient(app) as client:
        yield LedgerService(client=client, framework=framework, gate=gate)


@pytest.fixture
def put_ledger(monkeypatch: pytest.MonkeyPatch) -> Iterator[LedgerService]:
    """A ledger with no registrations and the default double."""
    with _ledger_service(monkeypatch, ScriptedPolicyGate()) as ledger:
        yield ledger


async def test_the_ledger_registers_the_mock_by_put_and_it_decides_holds_and_replays(
    put_ledger: LedgerService, t0: datetime
):
    ledger = put_ledger
    assert ledger.framework.reasoners == ()
    missing = ledger.get()
    assert missing.status_code == 404
    assert missing.json()["code"] == "REASONER_NOT_FOUND"

    created = ledger.put(key=OPERATOR_KEY)
    assert created.status_code == 201, created.text
    assert created.headers["location"] == f"/api/v1/reasoners/{REASONER_ID}"
    view = created.json()
    assert (view["source"], view["registered_by"], view["registration_version"]) == (
        "api",
        "operator",
        VERSION,
    )
    unchanged = ledger.put(key=OPERATOR_KEY)
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["registration_version"] == VERSION
    served = ledger.get(key=MOCK_KEY)
    assert served.status_code == 200
    assert (served.json()["source"], served.json()["registration_version"]) == (
        "api",
        VERSION,
    )
    # The mock's own key reads its registration but may not write one.
    refused = ledger.put(key=MOCK_KEY)
    assert refused.status_code == 403
    assert refused.json()["code"] == "OPERATOR_REQUIRED"
    # The double admitted the binding once: the corpus at its tier.
    [(admitted, binding)] = ledger.gate.bindings
    assert admitted == REASONER_ID
    assert [(c.corpus_id, c.tier) for c in binding.corpora] == [
        (MOCK_STREAM_CORPUS_ID, MOCK_STREAM_CORPUS_TIER)
    ]

    payloads = synthetic_stream(PUT_STREAM_COUNT, run_tag="byput", start=t0)
    async with _mock_service(lambda _settings: ledger.http_client()) as service:
        summary = await _replay(service, payloads)

    picks = _checked(summary, payloads)
    assert len(ledger.gate.requests) == sum(
        d["mode"] == "gate" for d in summary.decisions
    )
    async with ledger.http_client() as client:
        for decision in summary.decisions:
            bundle = await client.get_bundle(decision["decision_id"])
            assert bundle.submission.event_id == decision["event_id"]
            assert bundle.registration_version == VERSION
        for kind, decision in picks.items():
            replay = await client.replay(decision["decision_id"])
            assert replay.matched, kind
            assert replay.registration_version == VERSION
    assert ledger.framework.registry.entry(REASONER_ID).source == "api"


def test_a_put_whose_binding_the_policy_gate_refuses_registers_nothing(
    monkeypatch: pytest.MonkeyPatch,
):
    gate = ScriptedPolicyGate(loaded_corpora=frozenset())
    with _ledger_service(monkeypatch, gate) as ledger:
        refused = ledger.put(key=OPERATOR_KEY)
        assert refused.status_code == 422
        problem = refused.json()
        assert (problem["code"], problem["gate_id"], problem["reasoner_id"]) == (
            "GATE_BINDING_REFUSED",
            "policy",
            REASONER_ID,
        )
        assert MOCK_STREAM_CORPUS_ID in problem["detail"]
        assert ledger.get().status_code == 404

    assert gate.bindings == []
    assert not ledger.framework.registry.is_registered(REASONER_ID)
    assert ledger.framework.store.snapshots.versions(REASONER_ID) == []
