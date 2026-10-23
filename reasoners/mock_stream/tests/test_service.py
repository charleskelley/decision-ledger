"""The stream mock's own service: produce into the stream, watch it decide.

``create_app`` runs here with its lifespan in the test's event loop (the
consumer task and the stream's queue belong to one loop) and a client over
the ledger in memory; requests go through ``httpx.ASGITransport``:

- ``POST /api/v1/stream/events`` takes an array or a single event and
  answers what the stream did with each (accepted, a dropped re-delivery,
  dead-lettered with the reason); a body that is not JSON, or is JSON but
  neither an event nor an array, is refused with 422;
- once the consumer has caught up, ``/decisions`` lists the decisions with
  their modes and actions, ``/dead-letters`` the malformed and late events
  with why, and ``/stats`` the counters and the watermark;
- ``/reset`` forgets the accounts, the dedup set, and the watermark, and
  keeps the dead letters;
- shutdown drains the events already accepted, then closes the client;
- without settings, a blank ``MOCK_STREAM_LEDGER_URL`` fails the service at
  startup; ``ledger_client`` calls the configured ledger over HTTP with the
  mock's key.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.auth import KEY_HEADER
from app.client import HttpClient, LocalClient
from mock_stream_reasoner.app import create_app, ledger_client
from mock_stream_reasoner.settings import MockStreamSettings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from fastapi import FastAPI

    from app.api.models import DecisionReceipt
    from app.plugin import FrameworkServices
    from core.submission import Submission
    from mock_stream_reasoner.events import TransferEvent

EVENTS = "/api/v1/stream/events"
STATS = "/api/v1/stream/stats"
DECISIONS = "/api/v1/stream/decisions"
DEAD_LETTERS = "/api/v1/stream/dead-letters"
RESET = "/api/v1/stream/reset"

#: A payload the stream cannot read as a transfer.
MALFORMED = {"event_id": "evt-bad", "account_id": "acct-steady", "amount": "lots"}


@pytest.fixture
def settings() -> MockStreamSettings:
    return MockStreamSettings(ledger_url="http://ledger.test", _env_file=None)


@asynccontextmanager
async def running(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan in this loop and talk to it over ASGI."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://mock.test"
        ) as http:
            yield http


@pytest.fixture
async def service(
    ledger: FrameworkServices, settings: MockStreamSettings
) -> AsyncIterator[httpx.AsyncClient]:
    """The mock's service, submitting to the ledger in memory."""
    app = create_app(settings, client_factory=lambda s: LocalClient(ledger))
    async with running(app) as http:
        yield http


async def caught_up(http: httpx.AsyncClient, *, timeout: float = 10.0) -> Any:
    """Poll ``/stats`` until the consumer has decided everything accepted."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        stats = (await http.get(STATS)).raise_for_status().json()
        if stats["caught_up"]:
            return stats
        assert loop.time() < deadline, f"the consumer did not catch up: {stats}"
        await asyncio.sleep(0.01)


def _mixed_stream(transfer: Callable[..., TransferEvent]) -> list[Any]:
    """A steady transfer, a fresh account's large one, a malformed, a late one."""
    return [
        transfer(1, "acct-steady", 100.0).payload(),
        transfer(2, "acct-fresh", 450.0, minutes=20).payload(),
        MALFORMED,
        # Fifty minutes behind the watermark (allowed lateness: five).
        transfer(3, "acct-late", 100.0, minutes=-30).payload(),
    ]


async def test_the_service_reports_its_health(service: httpx.AsyncClient):
    response = await service.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "reasoner_id": "mock-stream-reasoner"}


async def test_posting_an_array_answers_what_the_stream_did_with_each_event(
    service: httpx.AsyncClient, transfer: Callable[..., TransferEvent]
):
    first = transfer(1, "acct-a", 100.0).payload()
    body = [
        first,
        dict(first),  # a re-delivery of the same event id
        MALFORMED,
        transfer(2, "acct-a", 100.0, minutes=20).payload(),
        transfer(3, "acct-b", 100.0, minutes=-30).payload(),
    ]

    response = await service.post(EVENTS, json=body)

    assert response.status_code == 200
    assert response.json() == {
        "published": 5,
        "accepted": 2,
        "duplicates": 1,
        "dead_lettered": 2,
        "results": [
            {"event_id": "evt-test-0001", "outcome": "accepted", "reason": None},
            {"event_id": "evt-test-0001", "outcome": "duplicate", "reason": None},
            {"event_id": "evt-bad", "outcome": "dead_lettered", "reason": "malformed"},
            {"event_id": "evt-test-0002", "outcome": "accepted", "reason": None},
            {"event_id": "evt-test-0003", "outcome": "dead_lettered", "reason": "late"},
        ],
    }


async def test_a_single_event_object_is_published_like_an_array_of_one(
    service: httpx.AsyncClient, transfer: Callable[..., TransferEvent]
):
    response = await service.post(EVENTS, json=transfer(1, "acct-a", 100.0).payload())

    assert response.status_code == 200
    body = response.json()
    assert (body["published"], body["accepted"]) == (1, 1)
    assert body["results"] == [
        {"event_id": "evt-test-0001", "outcome": "accepted", "reason": None}
    ]


@pytest.mark.parametrize(
    ("content", "detail"),
    [
        pytest.param(b"{not json", "the body is not JSON", id="not-json"),
        pytest.param(
            b'"an event"', "the body must be an event or an array", id="json-string"
        ),
        pytest.param(b"42", "the body must be an event or an array", id="json-number"),
    ],
)
async def test_a_body_that_is_not_an_event_or_an_array_is_refused_with_422(
    service: httpx.AsyncClient, content: bytes, detail: str
):
    response = await service.post(
        EVENTS, content=content, headers={"content-type": "application/json"}
    )

    assert response.status_code == 422
    assert detail in response.json()["detail"]
    stats = (await service.get(STATS)).json()
    assert stats["source"]["received"] == 0


async def test_once_caught_up_the_service_lists_its_decisions_and_dead_letters(
    service: httpx.AsyncClient, transfer: Callable[..., TransferEvent]
):
    body = _mixed_stream(transfer)
    await service.post(EVENTS, json=body)
    await caught_up(service)

    decisions = (await service.get(DECISIONS)).json()
    assert [
        (
            d["event_id"],
            d["account_id"],
            d["mode"],
            d["decision_action"],
            d["bypass_rule_id"],
            d["decision_level_rule"],
            d["thin_history"],
        )
        for d in decisions
    ] == [
        (
            "evt-test-0001",
            "acct-steady",
            "record",
            "ALLOW",
            "steady-activity",
            None,
            True,
        ),
        (
            "evt-test-0002",
            "acct-fresh",
            "gate",
            "HOLD",
            None,
            "tier3_novel_entity",
            True,
        ),
    ]
    assert all(d["decision_id"] for d in decisions)
    latest = (await service.get(DECISIONS, params={"limit": 1})).json()
    assert latest == decisions[-1:]

    letters = (await service.get(DEAD_LETTERS)).json()
    assert [(letter["reason"], letter["event_id"]) for letter in letters] == [
        ("malformed", "evt-bad"),
        ("late", "evt-test-0003"),
    ]
    malformed, late = letters
    assert "timestamp: Field required" in malformed["detail"]
    assert "amount:" in malformed["detail"]
    assert malformed["payload"] == MALFORMED
    assert "behind the watermark" in late["detail"]
    assert late["payload"] == body[3]


async def test_stats_count_the_stream_and_its_decisions_and_carry_the_watermark(
    service: httpx.AsyncClient,
    transfer: Callable[..., TransferEvent],
    t0: datetime,
):
    await service.post(EVENTS, json=_mixed_stream(transfer))

    stats = await caught_up(service)

    assert stats["reasoner_id"] == "mock-stream-reasoner"
    assert stats["source"] == {
        "received": 4,
        "accepted": 2,
        "duplicates": 0,
        "dead_lettered": 2,
        "dead_lettered_malformed": 1,
        "dead_lettered_late": 1,
        "dead_lettered_rejected": 0,
        "dead_lettered_unreachable": 0,
        "dead_lettered_failed": 0,
    }
    assert stats["processor"] == {
        "action_ALLOW": 1,
        "action_HOLD": 1,
        "decided": 2,
        "mode_gate": 1,
        "mode_record": 1,
        "rule_tier3_novel_entity": 1,
    }
    assert (stats["pending"], stats["accounts"], stats["caught_up"]) == (0, 2, True)
    assert stats["history"] == 1000  # MOCK_STREAM_HISTORY's default
    assert datetime.fromisoformat(stats["watermark"]) == t0 + timedelta(minutes=20)


@pytest.mark.parametrize("history", [0, 100_001])
def test_a_history_outside_its_bounds_is_refused(history: int):
    with pytest.raises(ValidationError, match="history"):
        MockStreamSettings(
            ledger_url="http://ledger.test", history=history, _env_file=None
        )


async def test_reset_forgets_accounts_and_the_dedup_set_but_keeps_dead_letters(
    service: httpx.AsyncClient, transfer: Callable[..., TransferEvent]
):
    history = [transfer(n, "acct-a", 100.0, minutes=15 * n) for n in range(3)]
    await service.post(EVENTS, json=[e.payload() for e in history] + [MALFORMED])
    assert (await caught_up(service))["accounts"] == 1

    reset = (await service.post(RESET)).json()

    assert (reset["accounts"], reset["watermark"]) == (0, None)
    assert reset["source"]["dead_lettered_malformed"] == 1
    again = (await service.post(EVENTS, json=history[0].payload())).json()
    assert again["results"] == [
        {"event_id": "evt-test-0000", "outcome": "accepted", "reason": None}
    ]
    await service.post(EVENTS, json=transfer(3, "acct-a", 100.0, minutes=45).payload())
    await caught_up(service)
    decisions = (await service.get(DECISIONS)).json()
    # Without the reset, evt-test-0000 would be a dropped re-delivery and
    # evt-test-0003 the account's fourth transfer (no longer thin).
    assert [(d["event_id"], d["thin_history"]) for d in decisions[3:]] == [
        ("evt-test-0000", True),
        ("evt-test-0003", True),
    ]
    letters = (await service.get(DEAD_LETTERS)).json()
    assert [(letter["reason"], letter["event_id"]) for letter in letters] == [
        ("malformed", "evt-bad")
    ]


class _HeldLedger:
    """A ledger client double: holds every submission until released."""

    def __init__(self, inner: LocalClient) -> None:
        self.inner = inner
        self.release = asyncio.Event()
        self.calls: list[str] = []

    async def submit(self, submission: Submission) -> DecisionReceipt:
        await self.release.wait()
        self.calls.append(f"submit {submission.event_id}")
        return await self.inner.submit(submission)

    async def aclose(self) -> None:
        self.calls.append("aclose")


async def test_shutdown_drains_the_accepted_events_then_closes_the_client(
    ledger: FrameworkServices,
    settings: MockStreamSettings,
    transfer: Callable[..., TransferEvent],
):
    client = _HeldLedger(LocalClient(ledger))
    app = create_app(settings, client_factory=lambda s: client)
    events = [transfer(n, "acct-a", 100.0, minutes=15 * n).payload() for n in range(3)]

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://mock.test"
        ) as http:
            assert (await http.post(EVENTS, json=events)).json()["accepted"] == 3
            stats = (await http.get(STATS)).json()
            assert (stats["processor"], stats["caught_up"]) == ({}, False)
        # The ledger answers only once shutdown has begun: the stream is
        # closed first, and the consumer still decides what it accepted.
        asyncio.get_running_loop().call_soon(client.release.set)

    assert client.calls == [f"submit evt-test-{n:04d}" for n in range(3)] + ["aclose"]
    runtime = app.state.stream
    assert runtime.consumer.done()
    assert [d.event_id for d in runtime.processor.decisions] == [
        e["event_id"] for e in events
    ]
    assert runtime.source.pending == 0


async def test_without_settings_a_blank_ledger_url_fails_the_service_at_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.chdir(tmp_path)  # no .env is read
    monkeypatch.setenv("MOCK_STREAM_LEDGER_URL", "")
    app = create_app()  # settings are read at startup, not here
    lifespan = app.router.lifespan_context(app)

    with pytest.raises(ValidationError, match="MOCK_STREAM_LEDGER_URL is blank"):
        await lifespan.__aenter__()


@pytest.mark.parametrize(
    ("key", "sent"),
    [
        pytest.param(SecretStr("mock-stream-key"), "mock-stream-key", id="with-key"),
        pytest.param(SecretStr(""), None, id="blank-key"),
    ],
)
async def test_the_ledger_client_calls_the_configured_ledger_with_the_mocks_key(
    monkeypatch: pytest.MonkeyPatch, key: SecretStr, sent: str | None
):
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"decision_id": "d-1"})

    network = httpx.AsyncClient

    class _NoNetwork(network):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**{**kwargs, "transport": httpx.MockTransport(answer)})

    monkeypatch.setattr(httpx, "AsyncClient", _NoNetwork)
    settings = MockStreamSettings(
        ledger_url="http://ledger.test:8000/", ledger_key=key, _env_file=None
    )

    client = ledger_client(settings)
    try:
        assert isinstance(client, HttpClient)
        assert await client.get_decision("d-1") == {"decision_id": "d-1"}
    finally:
        await client.aclose()

    [request] = seen
    assert str(request.url) == "http://ledger.test:8000/api/v1/decisions/d-1"
    assert request.headers.get(KEY_HEADER) == sent
