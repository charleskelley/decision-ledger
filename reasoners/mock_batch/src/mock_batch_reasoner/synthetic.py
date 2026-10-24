"""A synthetic day — the batch mock's deterministic overnight extract.

``synthetic_day`` writes one row per account for a business date: the same
arguments always give the same rows. Each row is drawn from one of five
profiles, shaped so its score lands in one bypass rule's band:

- ``current`` (most accounts): low utilization, nothing past due — records
  ALLOW (``current-account``);
- ``watch``: high utilization, 30 days past due — records CHALLENGE
  (``watch-list``);
- ``delinquent``: at or near the limit, 60 days past due — records HOLD
  (``delinquent-hold``);
- ``charge_off``: over the limit, 90 days or more past due, payments
  returned — records BLOCK (``charge-off``);
- ``flagged`` (about one row in a hundred): any of the four, flagged for
  review upstream (``needs_review``) — matches no rule, so it is an
  exception, submitted in gate mode for the rules gate to decide.

The first ``MIN_COUNT`` rows are one of each profile, in that order, so every
day of at least ``MIN_COUNT`` rows covers every band and holds an exception.
Account ids are stable across days (``acct-000001`` is the same account
every day); row ids carry the run tag, so two runs never share an
``event_id``.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING

from mock_batch_reasoner.rows import AccountDay

if TYPE_CHECKING:
    from collections.abc import Callable

#: Rows in a default run: three batch requests of at most 10,000 lines.
DEFAULT_COUNT = 25_000
#: The fewest rows a synthetic day has: one of each profile.
MIN_COUNT = 5
DEFAULT_SEED = 7

#: The time of day a business date's positions are taken.
CUT_OFF = time(23, 59, 59)

_LIMITS = (500.0, 1_000.0, 2_500.0, 5_000.0, 10_000.0)
_PROFILES = ("current", "watch", "delinquent", "charge_off", "flagged")
_WEIGHTS = (0.81, 0.10, 0.05, 0.03, 0.01)


def _position(
    rng: random.Random, utilization: tuple[float, float]
) -> tuple[float, float]:
    """A credit limit and a balance at a utilization drawn from the range."""
    limit = rng.choice(_LIMITS)
    return limit, round(limit * rng.uniform(*utilization), 2)


def _current(rng: random.Random) -> tuple[float, float, int, int]:
    limit, balance = _position(rng, (0.0, 0.6))
    return limit, balance, 0, 1 if rng.random() < 0.05 else 0


def _watch(rng: random.Random) -> tuple[float, float, int, int]:
    limit, balance = _position(rng, (0.7, 1.0))
    return limit, balance, 30, 0


def _delinquent(rng: random.Random) -> tuple[float, float, int, int]:
    limit, balance = _position(rng, (0.8, 1.1))
    return limit, balance, 60, 0


def _charge_off(rng: random.Random) -> tuple[float, float, int, int]:
    limit, balance = _position(rng, (0.9, 1.3))
    return limit, balance, rng.choice((90, 120)), rng.choice((1, 2))


_DRAWS: dict[str, Callable[[random.Random], tuple[float, float, int, int]]] = {
    "current": _current,
    "watch": _watch,
    "delinquent": _delinquent,
    "charge_off": _charge_off,
}


def synthetic_day(
    count: int = DEFAULT_COUNT,
    *,
    business_date: date,
    run_tag: str,
    seed: int = DEFAULT_SEED,
) -> list[AccountDay]:
    """A deterministic day of account positions.

    Args:
        count: Rows (one per account), at least ``MIN_COUNT``.
        business_date: The day reviewed; every row's ``as_of`` is its
            ``CUT_OFF`` in UTC.
        run_tag: The prefix of this run's row ids
            (``row-<run_tag>-<n>``).
        seed: The random seed; the same arguments give the same rows.

    Returns:
        ``count`` rows, for accounts ``acct-000001`` onward.

    Raises:
        ValueError: ``count`` is below ``MIN_COUNT``.
    """
    if count < MIN_COUNT:
        msg = f"a synthetic day has at least {MIN_COUNT} rows, got {count}"
        raise ValueError(msg)
    rng = random.Random(seed)
    as_of = datetime.combine(business_date, CUT_OFF, tzinfo=UTC)
    rows: list[AccountDay] = []
    for n in range(1, count + 1):
        profile = (
            _PROFILES[n - 1]
            if n <= MIN_COUNT
            else rng.choices(_PROFILES, weights=_WEIGHTS)[0]
        )
        flagged = profile == "flagged"
        if flagged:
            profile = rng.choice(tuple(_DRAWS))
        limit, balance, days_past_due, returned = _DRAWS[profile](rng)
        rows.append(
            AccountDay(
                event_id=f"row-{run_tag}-{n:06d}",
                as_of=as_of,
                account_id=f"acct-{n:06d}",
                balance=balance,
                credit_limit=limit,
                days_past_due=days_past_due,
                returned_payments=returned,
                needs_review=flagged,
            )
        )
    return rows
