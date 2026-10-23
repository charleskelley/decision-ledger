"""The synthetic stream, and replaying it into the mock's service.

``synthetic_stream`` always writes the same stream for a seed, a run tag,
and a start, and another tag writes a stream with no event or account in
common. The default 200 events, fed through the mock's own ``EventSource``,
are 195 accepted transfers, 3 dropped re-deliveries, 1 late event, and 1
malformed payload; scored through the per-account windows and assembled,
the transfers are 185 records (182 ALLOW, 3 BLOCK) and 10 gated, 3 of them
the large first transfers of fresh accounts (a thin history). The shortest
stream allowed (``MIN_COUNT``) still carries every kind of account and every
fault; a shorter one, or one with a naive start, is refused.

``replay_events`` posts a stream to the mock's service (its lifespan in the
test's event loop, submitting to the ledger in memory with the scripted
policy double) and waits until it decided everything accepted: 195
decisions, three of them held by Tier 3, and only this run's decisions and
dead letters, however many runs the service has carried.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from app.client import LocalClient
from core.actions import DecisionAction
from mock_stream_reasoner.app import create_app
from mock_stream_reasoner.assembler import build_submission
from mock_stream_reasoner.replay import MIN_COUNT, replay_events, synthetic_stream
from mock_stream_reasoner.scorer import score_transfer
from mock_stream_reasoner.settings import MockStreamSettings
from mock_stream_reasoner.source import EventSource
from mock_stream_reasoner.state import AccountWindows

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from app.plugin import FrameworkServices
    from mock_stream_reasoner.replay import ReplaySummary

#: How the source takes the default 200-event stream.
STREAM_OUTCOMES = {
    "accepted": 195,
    "duplicate": 3,
    "dead_lettered:late": 1,
    "dead_lettered:malformed": 1,
}


async def _publish_all(payloads: list[dict[str, Any]]) -> tuple[EventSource, Counter]:
    source = EventSource()
    outcomes: Counter[str] = Counter()
    for payload in payloads:
        published = await source.publish(payload)
        key = published.outcome
        if published.reason is not None:
            key = f"{key}:{published.reason}"
        outcomes[key] += 1
    return source, outcomes


def test_the_stream_is_the_same_for_the_same_seed_tag_and_start(t0: datetime):
    first = synthetic_stream(200, seed=7, run_tag="run-a", start=t0)

    assert synthetic_stream(200, seed=7, run_tag="run-a", start=t0) == first
    assert len(first) == 200
    assert synthetic_stream(200, seed=8, run_tag="run-a", start=t0) != first


def test_another_run_tag_writes_a_stream_with_no_id_in_common(t0: datetime):
    first = synthetic_stream(200, run_tag="run-a", start=t0)
    other = synthetic_stream(200, run_tag="run-b", start=t0)

    assert other != first
    assert all("-run-a-" in p["event_id"] for p in first)
    assert not {p["event_id"] for p in first} & {p["event_id"] for p in other}
    assert not {p["account_id"] for p in first} & {p["account_id"] for p in other}


async def test_the_stream_feeds_the_source_its_transfers_and_every_fault(
    t0: datetime,
):
    source, outcomes = await _publish_all(
        synthetic_stream(200, run_tag="run-a", start=t0)
    )

    assert outcomes == STREAM_OUTCOMES
    assert [(d.reason, d.event_id) for d in source.dead_letters] == [
        ("malformed", "evt-run-a-malformed"),
        ("late", "evt-run-a-late"),
    ]
    assert source.pending == 195


async def test_the_accepted_transfers_are_185_records_and_10_gated_3_of_them_fresh(
    t0: datetime,
):
    source, _ = await _publish_all(synthetic_stream(200, run_tag="run-a", start=t0))
    windows = AccountWindows()
    modes: Counter[str] = Counter()
    recorded: Counter[DecisionAction | None] = Counter()
    gated_thin: list[str] = []

    for event in source.take_ready():
        score = score_transfer(windows.observe(event))
        submission = build_submission(event, score)
        modes[submission.mode] += 1
        if submission.mode == "record":
            recorded[submission.proposed_action] += 1
        elif score.features.thin_history:
            gated_thin.append(event.account_id)

    assert modes == {"record": 185, "gate": 10}
    assert recorded == {DecisionAction.ALLOW: 182, DecisionAction.BLOCK: 3}
    assert sorted(gated_thin) == [f"acct-run-a-fresh-{n:02d}" for n in range(3)]


def test_a_stream_shorter_than_the_minimum_is_refused(t0: datetime):
    assert MIN_COUNT == 45
    with pytest.raises(ValueError, match="at least 45 events, got 44"):
        synthetic_stream(MIN_COUNT - 1, run_tag="run-a", start=t0)


def test_a_stream_with_a_naive_start_is_refused(t0: datetime):
    naive = t0.replace(tzinfo=None)

    with pytest.raises(ValueError, match="start must be timezone-aware"):
        synthetic_stream(200, run_tag="run-a", start=naive)


async def test_the_shortest_stream_allowed_still_carries_every_kind_and_fault(
    t0: datetime,
):
    payloads = synthetic_stream(MIN_COUNT, run_tag="short", start=t0)

    assert len(payloads) == MIN_COUNT
    source, outcomes = await _publish_all(payloads)
    assert outcomes == {
        "accepted": 40,
        "duplicate": 3,
        "dead_lettered:late": 1,
        "dead_lettered:malformed": 1,
    }
    windows = AccountWindows()
    gated: list[tuple[str, bool]] = []
    for event in source.take_ready():
        score = score_transfer(windows.observe(event))
        if build_submission(event, score).mode == "gate":
            gated.append((event.account_id, score.features.thin_history))
    # The burst's middle (4), three fresh accounts' large first transfers
    # (thin), and three established accounts' large fifth transfers.
    assert len(gated) == 10
    assert sum(thin for _, thin in gated) == 3
    assert sum("-steady-" in account for account, _ in gated) == 3


@pytest.fixture
async def service(ledger: FrameworkServices) -> AsyncIterator[httpx.AsyncClient]:
    """The mock's service over the ledger in memory, its lifespan in this loop."""
    settings = MockStreamSettings(ledger_url="http://ledger.test", _env_file=None)
    app = create_app(settings, client_factory=lambda s: LocalClient(ledger))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://mock.test"
        ) as http:
            yield http


async def _replay(
    service: httpx.AsyncClient, run_tag: str, start: datetime
) -> ReplaySummary:
    payloads = synthetic_stream(200, run_tag=run_tag, start=start)
    return await replay_events(payloads, client=service, timeout=60.0, poll=0.01)


def _assert_one_run(summary: ReplaySummary, run_tag: str) -> None:
    assert summary.published == 200
    assert summary.outcomes == STREAM_OUTCOMES
    assert len(summary.decisions) == 195
    assert all(d["event_id"].startswith(f"evt-{run_tag}-") for d in summary.decisions)
    assert Counter(d["mode"] for d in summary.decisions) == {"record": 185, "gate": 10}
    held = [
        d for d in summary.decisions if d["decision_level_rule"] == "tier3_novel_entity"
    ]
    assert [(d["account_id"], d["mode"], d["decision_action"]) for d in held] == [
        (f"acct-{run_tag}-fresh-{n:02d}", "gate", "HOLD") for n in range(3)
    ]
    assert [(d["reason"], d["event_id"]) for d in summary.dead_letters] == [
        ("malformed", f"evt-{run_tag}-malformed"),
        ("late", f"evt-{run_tag}-late"),
    ]


async def test_a_replay_reports_what_the_service_decided_of_its_stream(
    service: httpx.AsyncClient, t0: datetime
):
    summary = await _replay(service, "run-a", t0)

    _assert_one_run(summary, "run-a")
    actions = Counter(d["decision_action"] for d in summary.decisions)
    assert actions == {"ALLOW": 182, "BLOCK": 3, "CHALLENGE": 7, "HOLD": 3}


async def test_a_replay_reports_only_its_own_run(
    service: httpx.AsyncClient, t0: datetime
):
    first = await _replay(service, "run-a", t0)
    stats = (await service.get("/api/v1/stream/stats")).json()
    after_first = datetime.fromisoformat(stats["watermark"]) + timedelta(minutes=1)

    second = await _replay(service, "run-b", after_first)

    _assert_one_run(first, "run-a")
    _assert_one_run(second, "run-b")
    first_ids = {d["event_id"] for d in first.decisions}
    assert not first_ids & {d["event_id"] for d in second.decisions}
    everything = (
        await service.get("/api/v1/stream/decisions", params={"limit": 10_000})
    ).json()
    assert len(everything) == 390
    letters = (await service.get("/api/v1/stream/dead-letters")).json()
    assert len(letters) == 4
