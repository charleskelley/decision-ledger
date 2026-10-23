"""A deterministic synthetic stream, and replaying it into the mock's service.

``synthetic_stream`` writes the raw payloads a broker would deliver for a
small population of imaginary accounts, in arrival order, so the mock's
pattern can be watched end to end. For a seed and a run tag it always writes
the same stream:

- **steady accounts** send transfers near their own baseline, at least
  fifteen minutes apart: their first ``THIN_HISTORY_EVENTS`` transfers have a
  thin history, every one of them lands in the low band, and the ledger
  records them ALLOW under ``steady-activity``;
- **fresh accounts** send one transfer each: a small one is recorded ALLOW
  (a thin history does not gate a transfer in the low band), and a large one
  asks the policy gate, where Tier 3 holds it for review whatever the gate
  permits (the account's history is thin);
- **large transfers** from a few established steady accounts (six times
  their baseline) ask the policy gate, which decides them;
- **a burst**: an account sends nine transfers within six minutes, the early
  ones recorded ALLOW, the middle ones asked of the gate, and the rest
  recorded BLOCK under ``activity-surge``;
- **stream faults**: three re-deliveries of events already sent (the source
  drops them), one event from long behind the watermark (dead-lettered as
  ``late``), and one malformed payload (dead-lettered as ``malformed``).

``replay_events`` posts a stream to a running service and waits until it
has decided everything it accepted.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from mock_stream_reasoner.state import REFERENCE_AMOUNT, THIN_HISTORY_EVENTS

if TYPE_CHECKING:
    import httpx

#: Events of the default stream (``make replay-mock_stream``).
DEFAULT_COUNT = 200
#: The shortest stream: every kind of account and fault fits, the three
#: established accounts included, each reaching its large fifth transfer.
MIN_COUNT = 45
#: The default seed: the stream's shape for a run tag.
DEFAULT_SEED = 7
#: At most this many steady accounts carry the background traffic (fewer in a
#: short stream, so each still sends the five transfers a large one needs).
STEADY_ACCOUNTS = 30
#: How many established steady accounts send one large transfer.
LARGE_TRANSFERS = 3
#: How many fresh accounts send a large first transfer (Tier 3 holds).
FRESH_LARGE_ACCOUNTS = 3
#: How many fresh accounts send a small first transfer.
FRESH_SMALL_ACCOUNTS = 6
#: Transfers in the burst.
BURST_TRANSFERS = 9
#: Re-deliveries of events already sent.
DUPLICATES = 3

#: The least time between two transfers of a steady account (outside the
#: sliding window, so a steady transfer never counts another).
_STEADY_SPACING = timedelta(minutes=15)
_BURST_SPACING = timedelta(seconds=40)


@dataclass
class _Draft:
    """One transfer before it becomes a payload."""

    at: datetime
    account_id: str
    amount: float
    event_id: str = ""


@dataclass
class _Account:
    """A steady account's running state while the stream is written."""

    account_id: str
    base: float
    next_at: datetime
    sent: list[float] = field(default_factory=list)

    @property
    def baseline(self) -> float:
        return sum(self.sent) / len(self.sent) if self.sent else REFERENCE_AMOUNT


def synthetic_stream(
    count: int = DEFAULT_COUNT,
    *,
    seed: int = DEFAULT_SEED,
    start: datetime | None = None,
    run_tag: str | None = None,
) -> list[dict[str, Any]]:
    """Write a synthetic stream of transfer payloads, in arrival order.

    Args:
        count: Payloads to write, faults included; at least ``MIN_COUNT``
            (45), so every
            kind of account and fault fits.
        seed: The stream's shape for a run tag (amounts and spacing).
        start: Event time of the first transfer (timezone-aware); now, to the
            minute, when omitted.
        run_tag: Prefix that keeps this run's event and account ids apart
            from earlier runs'; a fresh random tag when omitted.

    Returns:
        JSON payloads as a broker would deliver them: transfers, then
        re-deliveries, a late event, and a malformed one, interleaved.

    Raises:
        ValueError: ``count`` is below ``MIN_COUNT``, or ``start`` is naive.
    """
    if count < MIN_COUNT:
        msg = f"a synthetic stream needs at least {MIN_COUNT} events, got {count}"
        raise ValueError(msg)
    if start is None:
        start = datetime.now(UTC).replace(second=0, microsecond=0)
    if start.tzinfo is None:
        msg = "start must be timezone-aware"
        raise ValueError(msg)
    tag = run_tag or uuid.uuid4().hex[:6]
    rng = random.Random(f"{seed}:{tag}")
    faults = DUPLICATES + 2  # re-deliveries, one late, one malformed
    drafts: list[_Draft] = []

    # The burst account warms up with three transfers, then bursts.
    burst = f"acct-{tag}-burst"
    burst_at = start + timedelta(minutes=5)
    for n in range(THIN_HISTORY_EVENTS):
        drafts.append(_Draft(burst_at + n * _STEADY_SPACING, burst, 100.0))
    burst_start = burst_at + THIN_HISTORY_EVENTS * _STEADY_SPACING + _STEADY_SPACING
    for n in range(BURST_TRANSFERS):
        drafts.append(_Draft(burst_start + n * _BURST_SPACING, burst, 250.0))

    # Fresh accounts send one transfer each, spread over the stream.
    for n in range(FRESH_SMALL_ACCOUNTS):
        amount = round(rng.uniform(30.0, 90.0), 2)
        at = start + timedelta(minutes=10 + 23 * n)
        drafts.append(_Draft(at, f"acct-{tag}-new-{n:02d}", amount))
    for n in range(FRESH_LARGE_ACCOUNTS):
        amount = round(rng.uniform(350.0, 500.0), 2)
        at = start + timedelta(minutes=40 + 37 * n)
        drafts.append(_Draft(at, f"acct-{tag}-fresh-{n:02d}", amount))

    # Steady accounts carry the rest, round robin, each at its own pace; a
    # large transfer is an account's fifth, once its history is established.
    remaining = count - len(drafts) - faults
    accounts = min(
        STEADY_ACCOUNTS,
        max(LARGE_TRANSFERS, remaining // (THIN_HISTORY_EVENTS + 2)),
    )
    steady = [
        _Account(
            account_id=f"acct-{tag}-steady-{n:02d}",
            base=round(rng.uniform(40.0, 110.0), 2),
            next_at=start + timedelta(minutes=rng.randint(0, 14)),
        )
        for n in range(accounts)
    ]
    large_from = {account.account_id for account in steady[:LARGE_TRANSFERS]}
    turn = 0
    while remaining > 0:
        account = steady[turn % len(steady)]
        turn += 1
        large = (
            account.account_id in large_from
            and len(account.sent) == THIN_HISTORY_EVENTS + 1
        )
        if large:
            large_from.discard(account.account_id)
            amount = round(account.baseline * 6.0, 2)
        else:
            amount = round(account.base * rng.uniform(0.9, 1.1), 2)
        drafts.append(_Draft(account.next_at, account.account_id, amount))
        account.sent.append(amount)
        account.next_at += _STEADY_SPACING + timedelta(minutes=rng.randint(0, 20))
        remaining -= 1

    drafts.sort(key=lambda draft: (draft.at, draft.account_id))
    for n, draft in enumerate(drafts):
        draft.event_id = f"evt-{tag}-{n:04d}"
    payloads: list[dict[str, Any]] = [_payload(draft) for draft in drafts]

    # Faults, delivered among the transfers: re-deliveries of events from the
    # first half, each a third of the stream after its original; one event
    # far behind the watermark, last; and one malformed payload.
    spread = len(payloads)
    copies = []
    for n in range(DUPLICATES):
        original = (n + 1) * spread // (2 * (DUPLICATES + 1))
        copies.append((original + spread // 3, dict(payloads[original])))
    for position, copy in sorted(copies, key=lambda c: c[0], reverse=True):
        payloads.insert(position, copy)
    late = drafts[len(drafts) // 2]
    payloads.append(
        {
            "event_id": f"evt-{tag}-late",
            "timestamp": (late.at - timedelta(hours=1)).isoformat(),
            "account_id": late.account_id,
            "amount": 75.0,
        }
    )
    payloads.insert(
        len(payloads) // 3,
        {"event_id": f"evt-{tag}-malformed", "account_id": burst, "amount": "lots"},
    )
    return payloads


def _payload(draft: _Draft) -> dict[str, Any]:
    return {
        "event_id": draft.event_id,
        "timestamp": draft.at.isoformat(),
        "account_id": draft.account_id,
        "amount": draft.amount,
    }


@dataclass(frozen=True)
class ReplaySummary:
    """What a replay published and what the service decided of it.

    Attributes:
        published: Payloads posted.
        outcomes: How the source took them (``accepted``, ``duplicate``,
            ``dead_lettered:<reason>``).
        decisions: The service's decisions for this run's events.
        dead_letters: The dead letters for this run's events.
        missing: This run's accepted events the service no longer lists:
            neither a decision it still keeps (it keeps its last
            ``MOCK_STREAM_HISTORY``) nor a dead letter. The ledger holds
            their bundles all the same.
    """

    published: int
    outcomes: dict[str, int]
    decisions: list[dict[str, Any]]
    dead_letters: list[dict[str, Any]]
    missing: int = 0


async def replay_events(
    payloads: list[Any],
    *,
    client: httpx.AsyncClient,
    chunk: int = 50,
    timeout: float = 600.0,
    poll: float = 0.5,
) -> ReplaySummary:
    """Post payloads to the mock's service and wait until it decided them.

    Args:
        payloads: The stream, in arrival order (JSON values; one that is not
            an event is the service's to dead-letter).
        client: An ``httpx.AsyncClient`` whose base URL is the service.
        chunk: Payloads per ``POST /api/v1/stream/events``.
        timeout: Seconds to wait for the service to catch up.
        poll: Seconds between progress checks.

    Returns:
        The summary of this run.

    Raises:
        TimeoutError: The service did not catch up within ``timeout``.
        httpx.HTTPStatusError: The service refused a request.
    """
    outcomes: dict[str, int] = {}
    accepted: set[str] = set()
    for offset in range(0, len(payloads), max(chunk, 1)):
        response = await client.post(
            "/api/v1/stream/events", json=payloads[offset : offset + max(chunk, 1)]
        )
        response.raise_for_status()
        for result in response.json()["results"]:
            key = result["outcome"]
            if key == "accepted" and result.get("event_id"):
                accepted.add(result["event_id"])
            if result.get("reason"):
                key = f"{key}:{result['reason']}"
            outcomes[key] = outcomes.get(key, 0) + 1
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        stats = (await client.get("/api/v1/stream/stats")).raise_for_status().json()
        if stats["caught_up"]:
            break
        if loop.time() > deadline:
            msg = f"the service did not catch up within {timeout} s: {stats}"
            raise TimeoutError(msg)
        await asyncio.sleep(poll)
    ids = {
        p["event_id"]
        for p in payloads
        if isinstance(p, dict) and isinstance(p.get("event_id"), str)
    }
    listed = (
        (
            await client.get(
                "/api/v1/stream/decisions", params={"limit": stats["history"]}
            )
        )
        .raise_for_status()
        .json()
    )
    letters = (
        (await client.get("/api/v1/stream/dead-letters")).raise_for_status().json()
    )
    decisions = [d for d in listed if d["event_id"] in ids]
    dead_letters = [d for d in letters if d.get("event_id") in ids]
    accounted = {d["event_id"] for d in decisions} | {
        d["event_id"] for d in dead_letters
    }
    return ReplaySummary(
        published=len(payloads),
        outcomes=dict(sorted(outcomes.items())),
        decisions=decisions,
        dead_letters=dead_letters,
        missing=len(accepted - accounted),
    )
