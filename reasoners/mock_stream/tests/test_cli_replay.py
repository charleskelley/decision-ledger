"""``replay-events`` against a running service: the path the runbook follows.

The CLI's real replay path, not its dry run: it asks the service for its
watermark, starts the synthetic stream a minute after it, posts the stream,
waits, reports, and writes the run's decisions. The service runs in the
test's event loop over the ledger in memory; the CLI's ``httpx.AsyncClient``
is pointed at it with an ASGI transport.

- Two replays into one service are both decided in full: the second starts
  after the first's watermark, so none of it trails the watermark except the
  stream's own deliberate late event, and ``--decisions-out`` holds every
  decision of each run.
- A dry run asks the service for the watermark too when the service answers,
  and starts now when it cannot be reached.
- A replay larger than the service's history (``MOCK_STREAM_HISTORY``) says
  how many of its decisions the service no longer lists.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from app.client import LocalClient
from mock_stream_reasoner import __main__ as cli
from mock_stream_reasoner.app import create_app
from mock_stream_reasoner.settings import MockStreamSettings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from fastapi import FastAPI

    from app.plugin import FrameworkServices

SERVICE_URL = "http://mock.test"


def _args(**overrides: Any) -> argparse.Namespace:
    """The parsed ``replay-events`` arguments, defaults as the parser sets them."""
    values: dict[str, Any] = {
        "service_url": SERVICE_URL,
        "count": 60,
        "seed": 7,
        "run_tag": None,
        "start": None,
        "file": None,
        "chunk": 50,
        "timeout": 30.0,
        "decisions_out": None,
        "dry_run": False,
    }
    return argparse.Namespace(**{**values, **overrides})


def _route_to(monkeypatch: pytest.MonkeyPatch, app: FastAPI | None) -> None:
    """Point the CLI's ``httpx.AsyncClient`` at ``app`` (or at nothing)."""
    real = httpx.AsyncClient

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no service", request=request)

    class _Routed(real):
        def __init__(self, **kwargs: Any) -> None:
            transport = (
                httpx.ASGITransport(app=app)
                if app is not None
                else httpx.MockTransport(refuse)
            )
            super().__init__(**{**kwargs, "transport": transport})

    monkeypatch.setattr(cli.httpx, "AsyncClient", _Routed)


async def _service(ledger: FrameworkServices, *, history: int = 1000) -> FastAPI:
    settings = MockStreamSettings(
        ledger_url="http://ledger.test", history=history, _env_file=None
    )
    return create_app(settings, client_factory=lambda s: LocalClient(ledger))


@pytest.fixture
async def service(ledger: FrameworkServices) -> AsyncIterator[FastAPI]:
    """The mock's service over the ledger in memory, its lifespan in this loop."""
    app = await _service(ledger)
    async with app.router.lifespan_context(app):
        yield app


def _report(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out)


async def test_two_replays_into_one_service_are_both_decided_in_full(
    service: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
):
    _route_to(monkeypatch, service)
    runs = []
    for tag in ("cli-one", "cli-two"):
        out = tmp_path / f"{tag}.ndjson"
        assert await cli._replay(_args(run_tag=tag, decisions_out=out)) == 0
        runs.append((_report(capsys), out))

    for (report, out), tag in zip(runs, ("cli-one", "cli-two"), strict=True):
        assert report["run_tag"] == tag
        assert report["outcomes"] == {
            "accepted": 55,
            "duplicate": 3,
            "dead_lettered:late": 1,
            "dead_lettered:malformed": 1,
        }
        assert report["decided"] == 55
        assert report["decisions_missing"] == 0
        written = [json.loads(line) for line in out.read_text().splitlines()]
        assert len(written) == 55
        assert all(f"-{tag}-" in decision["event_id"] for decision in written)
    assert [letter["event_id"] for letter in runs[1][0]["dead_letters"]] == [
        "evt-cli-two-malformed",
        "evt-cli-two-late",
    ]


async def test_a_dry_run_starts_after_the_watermark_when_the_service_answers(
    service: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _route_to(monkeypatch, service)
    assert await cli._replay(_args(run_tag="cli-first")) == 0
    capsys.readouterr()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=service), base_url=SERVICE_URL
    ) as http:
        watermark = datetime.fromisoformat(
            (await http.get("/api/v1/stream/stats")).json()["watermark"]
        )

    assert await cli._replay(_args(run_tag="cli-dry", dry_run=True)) == 0

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(lines) == 60
    # The stream starts a minute after the watermark (its first transfers
    # begin within its first quarter hour), so nothing in it trails it.
    times = [datetime.fromisoformat(p["timestamp"]) for p in lines if "timestamp" in p]
    late = [p for p in lines if p["event_id"].endswith("-late")]
    on_time = [t for t, p in zip(times, lines, strict=False) if p not in late]
    assert watermark + timedelta(minutes=1) <= min(on_time)
    assert min(on_time) <= watermark + timedelta(minutes=16)


async def test_a_dry_run_that_cannot_reach_the_service_starts_now(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _route_to(monkeypatch, None)
    before = datetime.now(UTC).replace(second=0, microsecond=0)

    assert await cli._replay(_args(run_tag="cli-offline", dry_run=True)) == 0

    first = json.loads(capsys.readouterr().out.splitlines()[0])
    started = datetime.fromisoformat(first["timestamp"])
    assert before <= started <= before + timedelta(minutes=2)


async def test_a_replay_that_cannot_reach_the_service_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    _route_to(monkeypatch, None)

    with pytest.raises(httpx.ConnectError):
        await cli._replay(_args(run_tag="cli-down"))


async def test_a_replay_beyond_the_services_history_reports_what_it_no_longer_lists(
    ledger: FrameworkServices,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    app = await _service(ledger, history=20)
    async with app.router.lifespan_context(app):
        _route_to(monkeypatch, app)
        assert await cli._replay(_args(run_tag="cli-long")) == 0

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["outcomes"]["accepted"] == 55
    assert report["decided"] == 20
    assert report["decisions_missing"] == 35
    assert "35 of this run's decisions are no longer listed" in captured.err
    assert "MOCK_STREAM_HISTORY" in captured.err


async def test_a_file_with_a_line_that_is_not_an_event_object_is_replayed_and_reported(
    service: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
):
    _route_to(monkeypatch, service)
    stream = tmp_path / "stream.ndjson"
    transfer = {
        "event_id": "evt-file-1",
        "timestamp": "2026-10-01T09:00:00+00:00",
        "account_id": "acct-file",
        "amount": 50.0,
    }
    stream.write_text(
        json.dumps(transfer) + "\n" + json.dumps("a stray string") + "\n42\n"
    )

    assert await cli._replay(_args(file=stream)) == 0

    report = _report(capsys)
    assert report["run_tag"] is None
    assert report["outcomes"] == {"accepted": 1, "dead_lettered:malformed": 2}
    assert report["decided"] == 1
    assert report["decisions_missing"] == 0
